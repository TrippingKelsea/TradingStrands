"""Dashboard API — FastAPI routes for reading trading state from DynamoDB."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="TradingStrands Dashboard")

_STATIC_DIR = Path(__file__).parent / "static"


def _get_table_name() -> str:
    return os.environ.get("DYNAMODB_TABLE", "trading-strands-state")


def _get_table() -> Any:
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(_get_table_name())


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text())


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
    resp = table.query(
        KeyConditionExpression=Key("pk").begins_with("EVENT#"),
        ScanIndexForward=False,
        Limit=50,
    )
    return [dict(item) for item in resp.get("Items", [])]


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
    symbols: list[str]
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


@app.delete("/api/strategies/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: str) -> None:
    table = _get_table()
    table.delete_item(Key={"pk": f"STRATEGY#{strategy_id}"})
