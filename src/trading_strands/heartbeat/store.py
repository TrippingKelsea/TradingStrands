"""Heartbeat storage. One row per (agent_type, agent_id).

Schema:
    pk = HEARTBEAT#<agent_type>#<agent_id>
    agent_type, agent_id         — extracted back from the pk for readability
    last_beat_ts                 — unix seconds (UTC)
    ttl                          — unix seconds, 7 days out; DDB auto-deletes
    status                       — healthy | degraded | error (optional)
    current_activity             — short string (memory.flush, reasoning, ...)
    last_decision_at             — unix seconds, last decision tick
    memory_file_cursor           — bytes into today's memory file
    queue_depth                  — pending A2A messages inbound
    errors_last_hour             — count for trend

Beat = put_item overwrite. Scan = FilterExpression on pk prefix.
Small table, low write volume (one row per active agent, rewritten
per tick). No GSI.

The extended payload fields are per docs/SPEC/observability.md
§"Health checks". Callers that don't supply them still get a valid
bare heartbeat — the Platform Supervisor treats absence as "unknown"
rather than an error, so a v0 bot that doesn't yet emit the full
payload stays green.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from boto3.dynamodb.conditions import Attr

from trading_strands.ddb import scan_all

HEARTBEAT_PK_PREFIX = "HEARTBEAT#"

# Dormant entries clean themselves up after a week — a stopped
# strategy shouldn't stay in the heartbeat table forever.
_BEAT_TTL_SECONDS = 7 * 24 * 3600

_VALID_STATUSES = ("healthy", "degraded", "error")


@dataclass
class Heartbeat:
    agent_type: str
    agent_id: str
    last_beat_ts: int
    status: str = "healthy"
    current_activity: str = ""
    last_decision_at: int = 0
    memory_file_cursor: int = 0
    queue_depth: int = 0
    errors_last_hour: int = 0
    # Surface-level extensibility — supervisor + UI read a few known
    # fields directly; anything else a caller wants to attach flows
    # through here. Keep this dict small; high-cardinality data
    # belongs in its own store.
    extras: dict[str, str] = field(default_factory=dict)


def _pk(agent_type: str, agent_id: str) -> str:
    return f"{HEARTBEAT_PK_PREFIX}{agent_type}#{agent_id}"


class HeartbeatStore:
    """DDB-backed heartbeat writer + reader."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def beat(
        self,
        agent_type: str,
        agent_id: str,
        *,
        status: str = "healthy",
        current_activity: str = "",
        last_decision_at: int | None = None,
        memory_file_cursor: int = 0,
        queue_depth: int = 0,
        errors_last_hour: int = 0,
    ) -> None:
        """Record a single heartbeat. Idempotent overwrite.

        Only `agent_type` and `agent_id` are required — the rest of the
        payload is optional so existing callers keep working unchanged.
        Invalid `status` values coerce to 'healthy' rather than raise;
        a monitoring write is never worth a crash in the caller.
        """

        if status not in _VALID_STATUSES:
            status = "healthy"
        now = int(time.time())
        item: dict[str, Any] = {
            "pk": _pk(agent_type, agent_id),
            "agent_type": agent_type,
            "agent_id": agent_id,
            "last_beat_ts": now,
            "ttl": now + _BEAT_TTL_SECONDS,
            "status": status,
            "current_activity": current_activity,
            "last_decision_at": (
                int(last_decision_at)
                if last_decision_at is not None else 0
            ),
            "memory_file_cursor": int(memory_file_cursor),
            "queue_depth": int(queue_depth),
            "errors_last_hour": int(errors_last_hour),
        }
        self._table.put_item(Item=item)

    def list_all(self) -> list[Heartbeat]:
        """Every recorded heartbeat. Filters on pk prefix so siblings
        in the shared table don't leak in."""

        items = scan_all(
            self._table, Attr("pk").begins_with(HEARTBEAT_PK_PREFIX),
        )
        return [
            Heartbeat(
                agent_type=str(item.get("agent_type", "")),
                agent_id=str(item.get("agent_id", "")),
                last_beat_ts=int(item.get("last_beat_ts", 0)),
                status=str(item.get("status", "healthy")),
                current_activity=str(item.get("current_activity", "")),
                last_decision_at=int(item.get("last_decision_at", 0)),
                memory_file_cursor=int(item.get("memory_file_cursor", 0)),
                queue_depth=int(item.get("queue_depth", 0)),
                errors_last_hour=int(item.get("errors_last_hour", 0)),
            )
            for item in items
        ]
