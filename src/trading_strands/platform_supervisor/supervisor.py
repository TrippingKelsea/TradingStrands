"""Platform Supervisor Lambda.

One invocation per EventBridge tick: scans the heartbeat table,
classifies each agent, returns a summary. Emits EMF metrics for each
count (healthy/stale/missing) so CloudWatch alarms can trigger on
missing > 0 without reading logs.

Thresholds default to stale=60s, missing=300s — the tick cadence for
strategy bots is 5s, so even a modest slowdown shouldn't trip stale;
missing catches bots that have actually stopped beating for a while.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from trading_strands.emf.emitter import emit_metric
from trading_strands.heartbeat.store import HeartbeatStore

logger = structlog.get_logger()


class AgentHealthStatus(Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    MISSING = "missing"


@dataclass
class AgentHealth:
    agent_type: str
    agent_id: str
    last_beat_ts: int
    status: AgentHealthStatus


@dataclass
class HealthReport:
    total: int
    healthy: int
    stale: int
    missing: int
    agents: list[AgentHealth] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Single-byte health: True iff no stale or missing agents."""

        return self.stale == 0 and self.missing == 0


def classify_beat(
    last_beat_ts: int,
    stale_after_seconds: float,
    missing_after_seconds: float,
) -> AgentHealthStatus:
    """Pure function. Zero or negative timestamps treated as missing —
    a heartbeat row whose last_beat_ts is unset or garbled means we
    have no evidence the agent has beat at all."""

    if last_beat_ts <= 0:
        return AgentHealthStatus.MISSING
    age = time.time() - last_beat_ts
    if age >= missing_after_seconds:
        return AgentHealthStatus.MISSING
    if age >= stale_after_seconds:
        return AgentHealthStatus.STALE
    return AgentHealthStatus.HEALTHY


def check_health(
    *,
    heartbeat_store: HeartbeatStore,
    stale_after_seconds: float,
    missing_after_seconds: float,
) -> HealthReport:
    """Walk every heartbeat, return a classified report.

    Agents list is sorted (agent_type, agent_id) for stable diffing
    across runs — operators compare consecutive reports by eye and
    shuffled ordering makes that hard.
    """

    beats = heartbeat_store.list_all()
    agents = [
        AgentHealth(
            agent_type=b.agent_type,
            agent_id=b.agent_id,
            last_beat_ts=b.last_beat_ts,
            status=classify_beat(
                b.last_beat_ts,
                stale_after_seconds,
                missing_after_seconds,
            ),
        )
        for b in beats
    ]
    agents.sort(key=lambda a: (a.agent_type, a.agent_id))

    healthy = sum(1 for a in agents if a.status is AgentHealthStatus.HEALTHY)
    stale = sum(1 for a in agents if a.status is AgentHealthStatus.STALE)
    missing = sum(1 for a in agents if a.status is AgentHealthStatus.MISSING)
    return HealthReport(
        total=len(agents),
        healthy=healthy, stale=stale, missing=missing,
        agents=agents,
    )


def _run(
    *,
    heartbeat_store: HeartbeatStore,
    stale_after_seconds: float,
    missing_after_seconds: float,
) -> dict[str, Any]:
    """Do the work. Split out from `handler` so tests can inject a
    pre-populated store without touching env/boto3."""

    report = check_health(
        heartbeat_store=heartbeat_store,
        stale_after_seconds=stale_after_seconds,
        missing_after_seconds=missing_after_seconds,
    )

    # EMF — one metric per bucket with a count. Dashboards + alarms
    # can slice by agent_type using the dimension.
    for status in AgentHealthStatus:
        count = sum(1 for a in report.agents if a.status is status)
        emit_metric(
            "supervisor.agents.count",
            value=count,
            unit="Count",
            dimensions={"status": status.value},
        )

    logger.info(
        "supervisor.complete total=%d healthy=%d stale=%d missing=%d",
        report.total, report.healthy, report.stale, report.missing,
    )

    return {
        "ok": report.ok,
        "total": report.total,
        "healthy": report.healthy,
        "stale": report.stale,
        "missing": report.missing,
        "agents": [
            {
                "agent_type": a.agent_type,
                "agent_id": a.agent_id,
                "last_beat_ts": a.last_beat_ts,
                "status": a.status.value,
            }
            for a in report.agents
        ],
    }


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """EventBridge entry. Reads thresholds from env, logs, returns.

    Env:
        DYNAMODB_TABLE
        SUPERVISOR_STALE_AFTER_SECONDS    default 60
        SUPERVISOR_MISSING_AFTER_SECONDS  default 300
    """

    import boto3

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(os.environ["DYNAMODB_TABLE"])
    store = HeartbeatStore(table)

    stale = float(os.environ.get("SUPERVISOR_STALE_AFTER_SECONDS", "60"))
    missing = float(os.environ.get("SUPERVISOR_MISSING_AFTER_SECONDS", "300"))

    return _run(
        heartbeat_store=store,
        stale_after_seconds=stale,
        missing_after_seconds=missing,
    )
