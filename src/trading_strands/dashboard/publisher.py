"""DynamoDB state publisher — writes trading state for the dashboard to read."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
import structlog

from trading_strands.ledger.models import Ledger
from trading_strands.risk.manager import RiskManager

logger = structlog.get_logger()


def _sanitize_floats(obj: Any) -> Any:
    """Recursively convert floats to Decimals for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_floats(v) for v in obj]
    return obj


def _decimal_default(obj: object) -> str | float:
    """Convert Decimal to string for DynamoDB compatibility."""
    if isinstance(obj, Decimal):
        return str(obj)
    msg = f"Object of type {type(obj)} is not serializable"
    raise TypeError(msg)


def _serialize_positions(ledger: Ledger) -> list[dict[str, str]]:
    return [
        {
            "symbol": pos.symbol,
            "quantity": str(pos.quantity),
            "burdened_cost_basis": str(pos.burdened_cost_basis),
        }
        for pos in ledger.open_positions
    ]


class StatePublisher:
    """Publishes trading state to DynamoDB for the dashboard to consume."""

    def __init__(self, table_name: str) -> None:
        self._table_name = table_name
        dynamodb = boto3.resource("dynamodb")
        self._table = dynamodb.Table(table_name)

    def publish_snapshot(
        self,
        tick: int,
        prices: dict[str, Decimal],
        ledgers: dict[str, Ledger],
        risk_manager: RiskManager,
        whatif_summary: dict[str, Any] | None = None,
        telemetry: dict[str, object] | None = None,
    ) -> None:
        """Write a full state snapshot to DynamoDB (overwrites previous)."""
        ledger_data: dict[str, Any] = {}
        for bot_id, ledger in ledgers.items():
            ledger_data[bot_id] = {
                "equity": str(ledger.equity),
                "realized_pnl": str(ledger.realized_pnl),
                "drawdown_pct": str(ledger.drawdown_pct),
                "high_water_mark": str(ledger.high_water_mark),
                "positions": _serialize_positions(ledger),
            }

        snapshot: dict[str, Any] = {
            "pk": "SNAPSHOT",
            "tick": tick,
            "timestamp": int(time.time()),
            "prices": {s: str(p) for s, p in prices.items()},
            "ledgers": ledger_data,
            "risk": {
                "desk_halted": risk_manager._desk_halted,
                "halted_bots": list(risk_manager._halted_bots),
            },
        }

        if whatif_summary is not None:
            snapshot["whatif"] = {
                k: str(v) if isinstance(v, Decimal) else v
                for k, v in whatif_summary.items()
            }

        if telemetry is not None:
            snapshot["telemetry"] = _sanitize_floats(telemetry)

        self._table.put_item(Item=snapshot)

    def publish_event(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Append a trade/risk event with 24h TTL for auto-cleanup."""
        ts = int(time.time())
        event_id = f"{ts}-{id(data)}"

        item: dict[str, Any] = {
            "pk": f"EVENT#{event_id}",
            "event_type": event_type,
            "timestamp": ts,
            "ttl": ts + 86400,  # 24h TTL
            "data": {
                k: str(v) if isinstance(v, Decimal) else v
                for k, v in data.items()
            },
        }

        self._table.put_item(Item=item)

    def put_strategy(
        self,
        name: str,
        markdown: str,
        symbols: list[str],
        capital: str = "1000",
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a strategy config in DynamoDB."""
        sid = strategy_id or str(uuid.uuid4())[:8]
        now = int(time.time())
        item: dict[str, Any] = {
            "pk": f"STRATEGY#{sid}",
            "strategy_id": sid,
            "name": name,
            "markdown": markdown,
            "symbols": symbols,
            "capital": capital,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self._table.put_item(Item=item)
        return item

    def get_strategies(self) -> list[dict[str, Any]]:
        """Scan for all strategy items."""
        resp = self._table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "STRATEGY#"},
        )
        return [dict(item) for item in resp.get("Items", [])]

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        """Get a single strategy by ID."""
        resp = self._table.get_item(Key={"pk": f"STRATEGY#{strategy_id}"})
        item = resp.get("Item")
        return dict(item) if item else None

    def update_strategy_status(self, strategy_id: str, status: str) -> bool:
        """Update a strategy's status (active/paused/stopped)."""
        try:
            self._table.update_item(
                Key={"pk": f"STRATEGY#{strategy_id}"},
                UpdateExpression="SET #s = :s, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status, ":t": int(time.time())},
                ConditionExpression="attribute_exists(pk)",
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def delete_strategy(self, strategy_id: str) -> None:
        """Delete a strategy from DynamoDB."""
        self._table.delete_item(Key={"pk": f"STRATEGY#{strategy_id}"})
