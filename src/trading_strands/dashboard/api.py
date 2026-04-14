"""Dashboard API — FastAPI routes for reading trading state from DynamoDB."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

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
