"""Heartbeat storage. One row per (agent_type, agent_id).

Schema:
    pk = HEARTBEAT#<agent_type>#<agent_id>
    agent_type, agent_id  — extracted back from the pk for readability
    last_beat_ts          — unix seconds (UTC)
    ttl                   — unix seconds, 7 days out; DDB auto-deletes

Beat = put_item overwrite. Scan = FilterExpression on pk prefix.
Small table, low write volume (one row per active agent, rewritten
per tick). No GSI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from boto3.dynamodb.conditions import Attr

HEARTBEAT_PK_PREFIX = "HEARTBEAT#"

# Dormant entries clean themselves up after a week — a stopped
# strategy shouldn't stay in the heartbeat table forever.
_BEAT_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class Heartbeat:
    agent_type: str
    agent_id: str
    last_beat_ts: int


def _pk(agent_type: str, agent_id: str) -> str:
    return f"{HEARTBEAT_PK_PREFIX}{agent_type}#{agent_id}"


class HeartbeatStore:
    """DDB-backed heartbeat writer + reader."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def beat(self, agent_type: str, agent_id: str) -> None:
        """Record a single heartbeat. Idempotent overwrite."""

        now = int(time.time())
        self._table.put_item(Item={
            "pk": _pk(agent_type, agent_id),
            "agent_type": agent_type,
            "agent_id": agent_id,
            "last_beat_ts": now,
            "ttl": now + _BEAT_TTL_SECONDS,
        })

    def list_all(self) -> list[Heartbeat]:
        """Every recorded heartbeat. Filters on pk prefix so siblings
        in the shared table don't leak in."""

        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with(HEARTBEAT_PK_PREFIX),
        )
        items = resp.get("Items", [])
        return [
            Heartbeat(
                agent_type=str(item.get("agent_type", "")),
                agent_id=str(item.get("agent_id", "")),
                last_beat_ts=int(item.get("last_beat_ts", 0)),
            )
            for item in items
        ]
