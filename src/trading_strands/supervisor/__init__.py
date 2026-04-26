"""StrategySupervisor — reacts to strategy row changes, reconciles Fargate.

Triggered by DynamoDB Streams on the strategies table. The only
component allowed to create/update/delete per-bot ECS services;
concentrating that IAM power in one place keeps the blast radius
small and auditing trivial.
"""

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
    "classify_record",
    "handler",
    "reconcile",
    "service_name_for",
]
