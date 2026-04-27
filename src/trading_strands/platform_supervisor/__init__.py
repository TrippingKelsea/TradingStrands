"""Platform Supervisor — v1 health monitor.

Scans the heartbeat table periodically, classifies each agent as
healthy / stale / missing against configurable thresholds. Emits
EMF metrics so CloudWatch dashboards and alarms can fire on the
same data the structured log records.

Distinct from v0's Orchestrator — this does NOT run the trade loop.
It only supervises other agents.
"""

from trading_strands.platform_supervisor.supervisor import (
    AgentHealth,
    AgentHealthStatus,
    HealthReport,
    check_health,
    classify_beat,
    handler,
)

__all__ = [
    "AgentHealth",
    "AgentHealthStatus",
    "HealthReport",
    "check_health",
    "classify_beat",
    "handler",
]
