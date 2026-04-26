"""One-shot reconciler: walk strategies table, make every active row
real in ECS. For cutover and disaster recovery.

Normally the StrategySupervisor reacts to DDB Stream events as they
happen. This one-shot reader:
    1. Walks `StrategyStore.list_all()`
    2. Synthesizes a `Decision` per active row (ENSURE_RUNNING)
    3. Runs each through the same `reconcile()` path Streams uses

Critical: this shares `reconcile()` with the streams handler so there
is exactly one code path that creates, updates, or deletes per-bot
services. Two paths would drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)
from trading_strands.supervisor.strategy_supervisor import (
    Action,
    Decision,
    reconcile,
)

logger = structlog.get_logger()


@dataclass
class ReconcileAllSummary:
    total: int
    reconciled: int
    errors: int
    dry_run_actions: list[str] = field(default_factory=list)


def decisions_from_store(store: StrategyStore) -> list[Decision]:
    """Synthesize the set of Decisions that would make ECS match the
    current strategy table — but only for ACTIVE rows with a real
    org_id. Paused/stopped rows need no service; legacy rows without
    org_id cannot be turned into single-bot tasks."""

    decisions: list[Decision] = []
    for s in store.list_all():
        if s.status != StrategyStatus.ACTIVE:
            continue
        if not s.org_id:
            logger.warning(
                "reconcile_all.skip_no_org_id strategy_id=%s", s.strategy_id,
            )
            continue
        decisions.append(Decision(
            action=Action.ENSURE_RUNNING,
            strategy_id=s.strategy_id,
            org_id=s.org_id,
        ))
    return decisions


def reconcile_all(
    *,
    store: StrategyStore,
    ecs_client: Any,
    cluster: str,
    base_task_definition_family: str,
    container_image: str,
    task_role_arn: str,
    execution_role_arn: str,
    subnet_ids: list[str],
    security_group_ids: list[str],
    log_group_name: str,
    region: str,
    extra_env: dict[str, str],
    dry_run: bool = False,
) -> ReconcileAllSummary:
    """Bring every active strategy onto per-bot Fargate.

    Operators invoke this at cutover (once, right after the supervisor
    Lambda is deployed with Streams=LATEST) so existing rows don't
    have to be pause-then-reactivated by hand.
    """

    decisions = decisions_from_store(store)
    summary = ReconcileAllSummary(
        total=len(decisions), reconciled=0, errors=0,
    )

    if dry_run:
        summary.dry_run_actions = [d.action.value for d in decisions]
        logger.info(
            "reconcile_all.dry_run total=%d", summary.total,
        )
        return summary

    for decision in decisions:
        try:
            reconcile(
                ecs_client=ecs_client,
                decision=decision,
                cluster=cluster,
                base_task_definition_family=base_task_definition_family,
                container_image=container_image,
                task_role_arn=task_role_arn,
                execution_role_arn=execution_role_arn,
                subnet_ids=subnet_ids,
                security_group_ids=security_group_ids,
                log_group_name=log_group_name,
                region=region,
                extra_env=extra_env,
            )
            summary.reconciled += 1
        except Exception:
            summary.errors += 1
            logger.exception(
                "reconcile_all.error strategy_id=%s", decision.strategy_id,
            )

    logger.info(
        "reconcile_all.complete total=%d reconciled=%d errors=%d",
        summary.total, summary.reconciled, summary.errors,
    )
    return summary


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point — same env as the streams handler, plus an
    optional `dry_run` boolean in the event payload.

    Designed to be invoked manually (aws lambda invoke) at cutover or
    for disaster-recovery runs. Not scheduled.
    """

    import os

    import boto3

    ddb = boto3.resource("dynamodb")
    ecs = boto3.client("ecs")
    table = ddb.Table(os.environ["DYNAMODB_TABLE"])
    store = StrategyStore(table)

    extra_env = {"DYNAMODB_TABLE": os.environ["DYNAMODB_TABLE"]}
    for k in ("AGENT_MEMORY_BUCKET", "SECRETS_MANAGER_SECRET_NAME"):
        v = os.environ.get(k)
        if v:
            extra_env[k] = v

    summary = reconcile_all(
        store=store,
        ecs_client=ecs,
        cluster=os.environ["ECS_CLUSTER"],
        base_task_definition_family=os.environ["TASK_DEFINITION_FAMILY"],
        container_image=os.environ["CONTAINER_IMAGE"],
        task_role_arn=os.environ["TASK_ROLE_ARN"],
        execution_role_arn=os.environ["EXECUTION_ROLE_ARN"],
        subnet_ids=os.environ["SUBNET_IDS"].split(","),
        security_group_ids=os.environ["SECURITY_GROUP_IDS"].split(","),
        log_group_name=os.environ["LOG_GROUP_NAME"],
        region=os.environ.get("AWS_REGION", "us-west-2"),
        extra_env=extra_env,
        dry_run=bool(event.get("dry_run", False)),
    )
    return {
        "ok": summary.errors == 0,
        "total": summary.total,
        "reconciled": summary.reconciled,
        "errors": summary.errors,
        "dry_run_actions": summary.dry_run_actions,
    }
