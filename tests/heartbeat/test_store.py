"""Tests for HeartbeatStore.

Heartbeats are a two-table-row dance: an agent calls beat() on each
tick; the Platform Supervisor scans items and flags any whose
last_beat_ts is older than a threshold. TTL on the items
auto-prunes dormant agents after a week — keeps the table clean
without manual bookkeeping.
"""

from __future__ import annotations

import time

import boto3
from moto import mock_aws

from trading_strands.heartbeat.store import (
    HEARTBEAT_PK_PREFIX,
    HeartbeatStore,
)


def _table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def test_beat_writes_item() -> None:
    with mock_aws():
        table = _table()
        store = HeartbeatStore(table)
        store.beat(agent_type="strategy", agent_id="strategy-abc")

        resp = table.get_item(
            Key={"pk": f"{HEARTBEAT_PK_PREFIX}strategy#strategy-abc"},
        )
        item = resp["Item"]
        assert item["agent_type"] == "strategy"
        assert item["agent_id"] == "strategy-abc"
        assert int(item["last_beat_ts"]) > 0
        # TTL is set so dormant entries clean up on their own.
        assert int(item["ttl"]) > int(item["last_beat_ts"])


def test_beat_updates_existing_item() -> None:
    """A new beat overwrites the prior one — we only ever need the
    most-recent timestamp."""

    with mock_aws():
        table = _table()
        store = HeartbeatStore(table)
        store.beat("strategy", "s-1")
        first = table.get_item(Key={
            "pk": f"{HEARTBEAT_PK_PREFIX}strategy#s-1",
        })["Item"]
        time.sleep(1.1)
        store.beat("strategy", "s-1")
        second = table.get_item(Key={
            "pk": f"{HEARTBEAT_PK_PREFIX}strategy#s-1",
        })["Item"]
        assert int(second["last_beat_ts"]) > int(first["last_beat_ts"])


def test_list_all_returns_every_agent() -> None:
    with mock_aws():
        table = _table()
        store = HeartbeatStore(table)
        store.beat("strategy", "s-1")
        store.beat("strategy", "s-2")
        store.beat("subscriber", "sub-1")

        beats = store.list_all()
        keys = {(b.agent_type, b.agent_id) for b in beats}
        assert keys == {
            ("strategy", "s-1"),
            ("strategy", "s-2"),
            ("subscriber", "sub-1"),
        }


def test_list_all_empty_when_no_heartbeats() -> None:
    with mock_aws():
        store = HeartbeatStore(_table())
        assert store.list_all() == []


def test_beat_persists_extended_payload() -> None:
    """docs/SPEC/observability.md §"Health checks" — the heartbeat-ack
    payload carries status + current_activity + memory_file_cursor +
    queue_depth + errors_last_hour so the supervisor can see a
    degrading trend before the bot actually stops beating."""

    with mock_aws():
        table = _table()
        store = HeartbeatStore(table)
        store.beat(
            agent_type="strategy", agent_id="s-extended",
            status="degraded",
            current_activity="memory.flush",
            last_decision_at=1_700_000_000,
            memory_file_cursor=4096,
            queue_depth=3,
            errors_last_hour=2,
        )
        [beat] = [
            b for b in store.list_all() if b.agent_id == "s-extended"
        ]
        assert beat.status == "degraded"
        assert beat.current_activity == "memory.flush"
        assert beat.last_decision_at == 1_700_000_000
        assert beat.memory_file_cursor == 4096
        assert beat.queue_depth == 3
        assert beat.errors_last_hour == 2


def test_invalid_status_coerced_to_healthy() -> None:
    """Monitoring writes must not crash the caller. An unexpected status
    string falls back to 'healthy' — the alternative is a dead agent
    that silently stops beating because its own heartbeat call raised."""

    with mock_aws():
        store = HeartbeatStore(_table())
        store.beat(
            "strategy", "s-bad-status",
            status="on_fire",  # type: ignore[arg-type]
        )
        [beat] = [
            b for b in store.list_all() if b.agent_id == "s-bad-status"
        ]
        assert beat.status == "healthy"


def test_bare_beat_backcompat() -> None:
    """Callers that still call beat(agent_type, agent_id) with no
    kwargs get a valid heartbeat with defaults — existing v0 bots
    don't need to be updated in lockstep."""

    with mock_aws():
        store = HeartbeatStore(_table())
        store.beat("auditor", "org-abc")
        [beat] = [b for b in store.list_all() if b.agent_id == "org-abc"]
        assert beat.status == "healthy"
        assert beat.current_activity == ""
        assert beat.errors_last_hour == 0


def test_list_all_ignores_non_heartbeat_rows() -> None:
    """Supervisor shares the table with everything else; it must not
    pick up STRATEGY#, ORG#, etc. as heartbeats."""

    with mock_aws():
        table = _table()
        # Non-heartbeat rows that could plausibly match a loose scan.
        table.put_item(Item={
            "pk": "STRATEGY#abc", "name": "x",
        })
        table.put_item(Item={
            "pk": "ORG#foo", "name": "y",
        })
        store = HeartbeatStore(table)
        store.beat("strategy", "s-1")
        beats = store.list_all()
        assert len(beats) == 1
        assert beats[0].agent_id == "s-1"
