"""Tests for the Platform Supervisor.

v1 health-monitor. Reads all heartbeats, classifies each agent as
healthy / stale / missing based on how long since last_beat_ts,
returns a status report. Lambda on a cron, logs the report and
emits EMF metrics.
"""

from __future__ import annotations

import time

import boto3
from moto import mock_aws

from trading_strands.heartbeat.store import HeartbeatStore
from trading_strands.platform_supervisor.supervisor import (
    AgentHealthStatus,
    HealthReport,
    check_health,
    classify_beat,
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


# ── classify_beat ───────────────────────────────────────────────────


def test_classify_fresh_is_healthy() -> None:
    now = time.time()
    status = classify_beat(
        last_beat_ts=int(now - 5),
        stale_after_seconds=60,
        missing_after_seconds=300,
    )
    assert status is AgentHealthStatus.HEALTHY


def test_classify_stale_when_past_stale_threshold() -> None:
    now = time.time()
    status = classify_beat(
        last_beat_ts=int(now - 120),
        stale_after_seconds=60,
        missing_after_seconds=300,
    )
    assert status is AgentHealthStatus.STALE


def test_classify_missing_when_past_missing_threshold() -> None:
    now = time.time()
    status = classify_beat(
        last_beat_ts=int(now - 600),
        stale_after_seconds=60,
        missing_after_seconds=300,
    )
    assert status is AgentHealthStatus.MISSING


def test_classify_zero_ts_is_missing() -> None:
    """A zero timestamp means an item with no valid beat recorded —
    treat as missing rather than throwing."""

    status = classify_beat(
        last_beat_ts=0,
        stale_after_seconds=60,
        missing_after_seconds=300,
    )
    assert status is AgentHealthStatus.MISSING


# ── check_health ────────────────────────────────────────────────────


def test_check_health_produces_a_report_per_agent() -> None:
    with mock_aws():
        table = _table()
        hb = HeartbeatStore(table)
        hb.beat("strategy", "s-fresh")
        # Manually stamp a stale beat by overwriting last_beat_ts.
        table.update_item(
            Key={"pk": "HEARTBEAT#strategy#s-stale"},
            UpdateExpression="SET agent_type=:t, agent_id=:a, last_beat_ts=:b, #ttl=:tt",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":t": "strategy", ":a": "s-stale",
                ":b": int(time.time() - 400),
                ":tt": int(time.time() + 3600),
            },
        )

        report = check_health(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert isinstance(report, HealthReport)
        assert report.total == 2
        ids = {a.agent_id: a.status for a in report.agents}
        assert ids["s-fresh"] is AgentHealthStatus.HEALTHY
        assert ids["s-stale"] is AgentHealthStatus.MISSING


def test_check_health_summarizes_counts() -> None:
    with mock_aws():
        hb = HeartbeatStore(_table())
        hb.beat("strategy", "s-1")
        hb.beat("strategy", "s-2")
        hb.beat("subscriber", "sub-1")

        report = check_health(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert report.healthy == 3
        assert report.stale == 0
        assert report.missing == 0


def test_check_health_ok_flag_reflects_issues() -> None:
    """ok=False when ANY agent is stale or missing. Callers use this
    as the single-byte health signal."""

    with mock_aws():
        table = _table()
        hb = HeartbeatStore(table)
        hb.beat("strategy", "s-1")  # fresh
        # Inject a missing entry directly.
        table.put_item(Item={
            "pk": "HEARTBEAT#strategy#s-dead",
            "agent_type": "strategy",
            "agent_id": "s-dead",
            "last_beat_ts": int(time.time() - 3600),
            "ttl": int(time.time() + 3600),
        })

        report = check_health(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert report.ok is False
        assert report.missing == 1
        assert report.healthy == 1


def test_check_health_empty_table_is_ok() -> None:
    """Zero agents = nothing to supervise. Report is ok — we don't
    want to alarm on an empty fleet (fresh deploy, all strategies
    stopped, etc.)."""

    with mock_aws():
        hb = HeartbeatStore(_table())
        report = check_health(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert report.total == 0
        assert report.ok is True


def test_check_health_ignores_non_fast_agent_types_for_classification() -> None:
    """Review agents (risk, compliance, auditor, self_critique) run on
    daily/weekly cadences — their heartbeats are always older than a
    tick-level stale threshold. We record them for UI purposes but
    don't count them toward the missing total that drives alerts.
    """

    with mock_aws():
        table = _table()
        hb = HeartbeatStore(table)
        # Fresh fast-cadence beat.
        hb.beat("strategy", "s-1")
        # Old slow-cadence beats — would be "missing" if not filtered.
        table.put_item(Item={
            "pk": "HEARTBEAT#risk#org-a",
            "agent_type": "risk",
            "agent_id": "org-a",
            "last_beat_ts": int(time.time() - 86400 * 3),
            "ttl": int(time.time() + 3600),
        })
        table.put_item(Item={
            "pk": "HEARTBEAT#self_critique#strategy-abc",
            "agent_type": "self_critique",
            "agent_id": "strategy-abc",
            "last_beat_ts": int(time.time() - 86400 * 5),
            "ttl": int(time.time() + 3600),
        })

        report = check_health(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        # All three beats surfaced in the agents list.
        assert report.total == 3
        # But only the strategy beat counts toward classification:
        # healthy=1, stale+missing from the fast-cadence perspective=0.
        assert report.healthy == 1
        assert report.stale == 0
        assert report.missing == 0
        assert report.ok is True

        # The review-agent rows are present in the agents list with a
        # distinct status so operators can still see them.
        risk_entry = next(a for a in report.agents if a.agent_type == "risk")
        assert risk_entry.status.value == "untracked"


# ── handler ─────────────────────────────────────────────────────────


def test_handler_returns_structured_summary() -> None:
    """Lambda wrapper — reads env for thresholds, wires up the store,
    returns the same fields the report carries."""

    from trading_strands.platform_supervisor import supervisor

    with mock_aws():
        table = _table()
        hb = HeartbeatStore(table)
        hb.beat("strategy", "s-1")
        # Inject a stale one.
        table.put_item(Item={
            "pk": "HEARTBEAT#strategy#s-slow",
            "agent_type": "strategy",
            "agent_id": "s-slow",
            "last_beat_ts": int(time.time() - 120),
            "ttl": int(time.time() + 3600),
        })

        result = supervisor._run(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert result["total"] == 2
        assert result["healthy"] == 1
        assert result["stale"] == 1
        assert result["missing"] == 0
        assert result["ok"] is False
        # Agents list is sorted for stable operator diffing.
        flagged = [a for a in result["agents"] if a["status"] != "healthy"]
        assert len(flagged) == 1
        assert flagged[0]["agent_id"] == "s-slow"


def test_handler_survives_beats_with_invalid_timestamps() -> None:
    """Defensive: a malformed heartbeat row (last_beat_ts missing or
    zero) must be classified as missing, not crash the run."""

    from trading_strands.platform_supervisor import supervisor

    with mock_aws():
        table = _table()
        table.put_item(Item={
            "pk": "HEARTBEAT#strategy#s-broken",
            "agent_type": "strategy",
            "agent_id": "s-broken",
            # No last_beat_ts at all.
            "ttl": int(time.time() + 3600),
        })
        hb = HeartbeatStore(table)

        result = supervisor._run(
            heartbeat_store=hb,
            stale_after_seconds=60,
            missing_after_seconds=300,
        )
        assert result["total"] == 1
        assert result["missing"] == 1
