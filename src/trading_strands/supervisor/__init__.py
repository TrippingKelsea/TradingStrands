"""StrategySupervisor — reacts to strategy row changes, reconciles Fargate.

Triggered by DynamoDB Streams on the strategies table. The only
component allowed to create/update/delete per-bot ECS services;
concentrating that IAM power in one place keeps the blast radius
small and auditing trivial.
"""

from trading_strands.supervisor.reconcile_all import (
    ReconcileAllSummary,
    decisions_from_store,
    reconcile_all,
)
from trading_strands.supervisor.strategy_supervisor import (
    Action,
    Decision,
    classify_record,
    handler,
    reconcile,
    service_name_for,
)

__all__ = [
    "Action",
    "Decision",
    "ReconcileAllSummary",
    "classify_record",
    "decisions_from_store",
    "handler",
    "reconcile",
    "reconcile_all",
    "service_name_for",
]
