"""Dashboard API — FastAPI routes for reading trading state from DynamoDB."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import boto3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from trading_strands.dashboard.auth import (
    SESSION_COOKIE,
    AuthMiddleware,
    authenticate,
    create_session_cookie,
)

app = FastAPI(title="TradingStrands Dashboard")

# Auth middleware — enforces login on all routes except /health, /login, /auth/*
app.add_middleware(AuthMiddleware)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _get_table_name() -> str:
    return os.environ.get("DYNAMODB_TABLE", "trading-strands-state")


def _get_table() -> Any:
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(_get_table_name())


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    error = request.query_params.get("error", "")
    return _templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/auth/login")
async def auth_login(
    email: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    user_info = authenticate(email, password)
    if user_info is None:
        return RedirectResponse(
            url="/login?" + urlencode({"error": "Invalid email or password"}),
            status_code=303,
        )

    session_value = create_session_cookie(user_info)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_value,
        max_age=86400 * 7,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.post("/auth/logout")
async def auth_logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "index.html")


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    table = _get_table()
    resp = table.get_item(Key={"pk": "SNAPSHOT"})
    item = resp.get("Item")
    if item is None:
        return {"tick": 0, "prices": {}, "ledgers": {}, "risk": {}, "timestamp": 0}
    return dict(item)


@app.get("/api/events")
async def events() -> list[dict[str, Any]]:
    table = _get_table()
    resp = table.scan(
        FilterExpression="begins_with(pk, :prefix)",
        ExpressionAttributeValues={":prefix": "EVENT#"},
    )
    items = resp.get("Items", [])
    # Sort by timestamp descending, return most recent 50
    items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return [dict(item) for item in items[:50]]


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    """SSE endpoint — polls DynamoDB every 2s and yields snapshots."""

    async def event_generator() -> Any:
        import json

        table = _get_table()
        last_tick = -1

        # Send initial connected message so clients know the stream is alive
        yield f"data: {json.dumps({'connected': True})}\n\n"

        while True:
            try:
                resp = table.get_item(Key={"pk": "SNAPSHOT"})
                item = resp.get("Item")
                if item is not None:
                    tick = item.get("tick", 0)
                    if tick != last_tick:
                        last_tick = tick
                        data = json.dumps(item, default=str)
                        yield f"data: {data}\n\n"
                else:
                    # No snapshot yet — send heartbeat to keep connection alive
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'read failed'})}\n\n"

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ── Strategy CRUD ───────────────────────────────────────────────────────


class StrategyCreate(BaseModel):
    name: str
    markdown: str
    symbols: list[str] = []
    capital: str = "1000"


class StrategyStatusUpdate(BaseModel):
    status: str  # active, paused, stopped


@app.get("/api/strategies")
async def list_strategies() -> list[dict[str, Any]]:
    table = _get_table()
    resp = table.scan(
        FilterExpression="begins_with(pk, :prefix)",
        ExpressionAttributeValues={":prefix": "STRATEGY#"},
    )
    items = resp.get("Items", [])
    return sorted(
        [dict(item) for item in items],
        key=lambda x: x.get("created_at", 0),
        reverse=True,
    )


@app.post("/api/strategies", status_code=201)
async def create_strategy(body: StrategyCreate) -> dict[str, Any]:
    import time
    import uuid

    table = _get_table()
    sid = str(uuid.uuid4())[:8]
    now = int(time.time())
    item: dict[str, Any] = {
        "pk": f"STRATEGY#{sid}",
        "strategy_id": sid,
        "name": body.name,
        "markdown": body.markdown,
        "symbols": body.symbols,
        "capital": body.capital,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    table.put_item(Item=item)
    return item


@app.put("/api/strategies/{strategy_id}/status")
async def update_strategy_status(
    strategy_id: str, body: StrategyStatusUpdate,
) -> dict[str, str]:
    if body.status not in ("active", "paused", "stopped"):
        raise HTTPException(status_code=400, detail="Invalid status")
    import time

    table = _get_table()
    try:
        table.update_item(
            Key={"pk": f"STRATEGY#{strategy_id}"},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": body.status, ":t": int(time.time())},
            ConditionExpression="attribute_exists(pk)",
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Strategy not found")  # noqa: B904
    return {"status": body.status}


class StrategyUpdate(BaseModel):
    name: str | None = None
    markdown: str | None = None
    symbols: list[str] | None = None
    capital: str | None = None


@app.get("/api/strategies/{strategy_id}")
async def get_strategy(strategy_id: str) -> dict[str, Any]:
    table = _get_table()
    resp = table.get_item(Key={"pk": f"STRATEGY#{strategy_id}"})
    item = resp.get("Item")
    if item is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return dict(item)


@app.put("/api/strategies/{strategy_id}")
async def update_strategy(
    strategy_id: str, body: StrategyUpdate,
) -> dict[str, Any]:
    import time

    table = _get_table()
    updates: list[str] = ["updated_at = :t"]
    names: dict[str, str] = {}
    values: dict[str, Any] = {":t": int(time.time())}

    if body.name is not None:
        updates.append("#n = :n")
        names["#n"] = "name"
        values[":n"] = body.name
    if body.markdown is not None:
        updates.append("markdown = :md")
        values[":md"] = body.markdown
    if body.symbols is not None:
        updates.append("symbols = :sym")
        values[":sym"] = body.symbols
    if body.capital is not None:
        updates.append("capital = :cap")
        values[":cap"] = body.capital

    try:
        kwargs: dict[str, Any] = {
            "Key": {"pk": f"STRATEGY#{strategy_id}"},
            "UpdateExpression": "SET " + ", ".join(updates),
            "ExpressionAttributeValues": values,
            "ConditionExpression": "attribute_exists(pk)",
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        resp = table.update_item(**kwargs)
    except Exception:
        raise HTTPException(status_code=404, detail="Strategy not found")  # noqa: B904
    return dict(resp.get("Attributes", {}))


@app.delete("/api/strategies/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: str) -> None:
    table = _get_table()
    table.delete_item(Key={"pk": f"STRATEGY#{strategy_id}"})


# ── Halt control ────────────────────────────────────────────────────────


@app.post("/api/halt")
async def halt_trading() -> dict[str, str]:
    """Emergency halt — writes desk halt flag to DynamoDB."""
    import time as _time

    table = _get_table()
    table.put_item(Item={
        "pk": "CONTROL",
        "desk_halted": True,
        "updated_at": int(_time.time()),
    })
    return {"status": "halted"}


@app.post("/api/unhalt")
async def unhalt_trading() -> dict[str, str]:
    """Resume trading — clears desk halt flag in DynamoDB."""
    import time as _time

    table = _get_table()
    table.put_item(Item={
        "pk": "CONTROL",
        "desk_halted": False,
        "updated_at": int(_time.time()),
    })
    return {"status": "running"}


# ── Telemetry ───────────────────────────────────────────────────────────


@app.get("/api/telemetry")
async def telemetry() -> dict[str, Any]:
    """Aggregate telemetry from dashboard-side checks and trading service snapshot."""
    import time as _time

    table = _get_table()
    now = int(_time.time())
    result: dict[str, Any] = {}

    # DynamoDB connectivity
    try:
        desc = table.meta.client.describe_table(TableName=table.table_name)
        tbl = desc.get("Table", {})
        result["dynamodb"] = {
            "status": "ok",
            "table_name": table.table_name,
            "table_status": tbl.get("TableStatus", "UNKNOWN"),
            "item_count": tbl.get("ItemCount", 0),
            "size_bytes": tbl.get("TableSizeBytes", 0),
        }
    except Exception as exc:
        result["dynamodb"] = {"status": "error", "error": str(exc)}

    # Trading service liveness (from snapshot)
    try:
        resp = table.get_item(Key={"pk": "SNAPSHOT"})
        item = resp.get("Item")
        if item:
            snap_ts = int(item.get("timestamp", 0))
            age = now - snap_ts
            result["trading_service"] = {
                "status": "ok" if age < 30 else "stale" if age < 120 else "down",
                "last_snapshot_age_seconds": age,
                "last_tick": item.get("tick", 0),
                "telemetry": item.get("telemetry", {}),
            }
        else:
            result["trading_service"] = {
                "status": "no_data",
                "last_snapshot_age_seconds": None,
                "last_tick": None,
                "telemetry": {},
            }
    except Exception as exc:
        result["trading_service"] = {"status": "error", "error": str(exc)}

    # Strategy counts
    try:
        strategies = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "STRATEGY#"},
            Select="ALL_ATTRIBUTES",
        ).get("Items", [])
        counts: dict[str, int] = {"active": 0, "paused": 0, "stopped": 0}
        for s in strategies:
            st = s.get("status", "unknown")
            counts[st] = counts.get(st, 0) + 1
        result["strategies"] = {"total": len(strategies), "by_status": counts}
    except Exception as exc:
        result["strategies"] = {"status": "error", "error": str(exc)}

    # Recent events count
    try:
        events_resp = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "EVENT#"},
            Select="COUNT",
        )
        result["events"] = {"recent_count": events_resp.get("Count", 0)}
    except Exception as exc:
        result["events"] = {"status": "error", "error": str(exc)}

    # Dashboard service info
    result["dashboard"] = {
        "status": "ok",
        "region": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "unknown")),
        "table_name": _get_table_name(),
    }

    return result
