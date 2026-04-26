"""StrategySupervisor Lambda: reconcile ECS services from DDB Streams.

Flow:
    1. DynamoDB Streams delivers a batch of STRATEGY# row changes.
    2. classify_record() turns each raw record into a Decision
       (ENSURE_RUNNING, PAUSE, STOP, or NOOP).
    3. reconcile() takes one Decision and makes it true in ECS:
       - ENSURE_RUNNING: register a per-strategy task definition, then
         create (or scale up) the service at desiredCount=1.
       - PAUSE: scale the existing service to desiredCount=0.
       - STOP: scale to 0, then delete the service.
       - NOOP: do nothing.

Why per-strategy task definitions: ECS services take env from the task
definition, not from the service itself. Passing STRATEGY_ID + ORG_ID
to the running container means registering a task definition revision
per strategy. We key this on a shared family and just bump revisions;
old revisions stay around for rollback and audit.

Safety invariants:
    - The supervisor is the ONLY component with ecs:CreateService /
      UpdateService / DeleteService on per-bot services.
    - STRATEGY_ID + ORG_ID are always set on the registered task def —
      omitting either would cause the bot to refuse to start, but we
      don't rely on that; we set both unconditionally here.
    - Per-record errors are caught inside the batch so one bad record
      doesn't force Streams to replay the whole batch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


class Action(Enum):
    NOOP = "NOOP"
    ENSURE_RUNNING = "ENSURE_RUNNING"
    PAUSE = "PAUSE"
    STOP = "STOP"


@dataclass(frozen=True)
class Decision:
    action: Action
    strategy_id: str | None
    org_id: str | None


def service_name_for(strategy_id: str) -> str:
    """One service per strategy, deterministic name."""

    return f"ts-strategy-{strategy_id}"


def _task_family_for(base_family: str, strategy_id: str) -> str:
    """Per-strategy family keeps revisions grouped per bot. A shared
    family would still work but would interleave revisions across bots
    and make rollback ambiguous."""

    return f"{base_family}-{strategy_id}"


# ── DDB Streams record parsing ──────────────────────────────────────


def _unwrap(image: dict[str, Any], key: str) -> str:
    """Extract a string value from a DDB attribute-value map."""

    v = image.get(key, {})
    if not isinstance(v, dict):
        return ""
    # DDB AttributeValue: {"S": "..."}, {"N": "..."}, {"BOOL": ...}
    for t in ("S", "N"):
        if t in v:
            return str(v[t])
    return ""


def classify_record(record: dict[str, Any]) -> Decision:
    """Turn one DDB Stream record into a Decision.

    Non-STRATEGY rows are NOOPs — the supervisor shares the streams
    filter with other consumers (bootstrap, etc.) in the future; it's
    cheaper to filter here than to maintain multiple filter rules.
    """

    event_name = record.get("eventName", "")
    dyn = record.get("dynamodb", {}) or {}
    new_image = dyn.get("NewImage") or {}
    old_image = dyn.get("OldImage") or {}

    # Pick an image — prefer new, fall back to old (for REMOVE).
    image = new_image or old_image
    pk = _unwrap(image, "pk")
    if not pk.startswith("STRATEGY#"):
        return Decision(Action.NOOP, None, None)

    strategy_id = _unwrap(image, "strategy_id") or pk.removeprefix("STRATEGY#")
    org_id = _unwrap(image, "org_id") or None

    if event_name == "REMOVE":
        # Strategy deleted — tear down.
        return Decision(Action.STOP, strategy_id, org_id)

    new_status = _unwrap(new_image, "status")

    if event_name == "INSERT":
        # Only born-active strategies need a service. Born-paused or
        # born-stopped are explicit "not running yet" states.
        if new_status == "active":
            return Decision(Action.ENSURE_RUNNING, strategy_id, org_id)
        return Decision(Action.NOOP, strategy_id, org_id)

    if event_name == "MODIFY":
        if not old_image:
            # Malformed stream event — stream view should always include
            # NEW_AND_OLD_IMAGES. Log and noop rather than guess.
            logger.warning("supervisor.modify_without_old_image pk=%s", pk)
            return Decision(Action.NOOP, strategy_id, org_id)
        old_status = _unwrap(old_image, "status")
        if old_status == new_status:
            return Decision(Action.NOOP, strategy_id, org_id)
        if new_status == "active":
            return Decision(Action.ENSURE_RUNNING, strategy_id, org_id)
        if new_status == "paused":
            return Decision(Action.PAUSE, strategy_id, org_id)
        if new_status == "stopped":
            return Decision(Action.STOP, strategy_id, org_id)
        return Decision(Action.NOOP, strategy_id, org_id)

    return Decision(Action.NOOP, strategy_id, org_id)


# ── ECS reconciliation ──────────────────────────────────────────────


def _service_state(
    ecs_client: Any, cluster: str, service_name: str,
) -> dict[str, Any] | None:
    """Return the service's current state dict, or None if missing."""

    resp = ecs_client.describe_services(
        cluster=cluster, services=[service_name],
    )
    services = resp.get("services", [])
    if not services:
        return None
    svc: dict[str, Any] = services[0]
    if svc.get("status") in ("MISSING", "INACTIVE", None):
        # Not an active service — treat as missing.
        return None
    return svc


def _register_task_def(
    ecs_client: Any,
    *,
    base_task_definition_family: str,
    strategy_id: str,
    org_id: str,
    container_image: str,
    task_role_arn: str,
    execution_role_arn: str,
    log_group_name: str,
    region: str,
    extra_env: dict[str, str],
) -> str:
    """Register a new task definition revision for this strategy.

    Returns the new revision's ARN. We pass STRATEGY_ID + ORG_ID as
    env — that's what the bot reads to boot into single-bot mode.
    """

    family = _task_family_for(base_task_definition_family, strategy_id)
    env = {**extra_env, "STRATEGY_ID": strategy_id, "ORG_ID": org_id}
    resp = ecs_client.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="512",
        memory="1024",
        taskRoleArn=task_role_arn,
        executionRoleArn=execution_role_arn,
        containerDefinitions=[
            {
                "name": "bot",
                "image": container_image,
                "essential": True,
                "environment": [{"name": k, "value": v} for k, v in env.items()],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group_name,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": f"strategy-{strategy_id}",
                    },
                },
            },
        ],
    )
    return str(resp["taskDefinition"]["taskDefinitionArn"])


def _ensure_running(
    ecs_client: Any,
    *,
    decision: Decision,
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
) -> None:
    assert decision.strategy_id is not None
    assert decision.org_id is not None
    service_name = service_name_for(decision.strategy_id)
    existing = _service_state(ecs_client, cluster, service_name)

    if existing is None:
        task_def_arn = _register_task_def(
            ecs_client,
            base_task_definition_family=base_task_definition_family,
            strategy_id=decision.strategy_id,
            org_id=decision.org_id,
            container_image=container_image,
            task_role_arn=task_role_arn,
            execution_role_arn=execution_role_arn,
            log_group_name=log_group_name,
            region=region,
            extra_env=extra_env,
        )
        ecs_client.create_service(
            cluster=cluster,
            serviceName=service_name,
            taskDefinition=task_def_arn,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": security_group_ids,
                    "assignPublicIp": "ENABLED",
                },
            },
        )
        logger.info(
            "supervisor.service_created service=%s strategy=%s",
            service_name, decision.strategy_id,
        )
        return

    # Service exists — scale up if it's sitting at 0.
    if int(existing.get("desiredCount", 0)) < 1:
        ecs_client.update_service(
            cluster=cluster, service=service_name, desiredCount=1,
        )
        logger.info(
            "supervisor.service_scaled_up service=%s", service_name,
        )


def _pause(ecs_client: Any, cluster: str, strategy_id: str) -> None:
    service_name = service_name_for(strategy_id)
    existing = _service_state(ecs_client, cluster, service_name)
    if existing is None:
        # Nothing to pause — the service never existed or was already
        # deleted. Log and move on.
        logger.info("supervisor.pause_noop service=%s", service_name)
        return
    ecs_client.update_service(
        cluster=cluster, service=service_name, desiredCount=0,
    )
    logger.info("supervisor.service_paused service=%s", service_name)


def _stop(ecs_client: Any, cluster: str, strategy_id: str) -> None:
    service_name = service_name_for(strategy_id)
    existing = _service_state(ecs_client, cluster, service_name)
    if existing is None:
        logger.info("supervisor.stop_noop service=%s", service_name)
        return
    # Belt and braces: scale to 0 first so the running task drains,
    # then delete. ECS DeleteService without force=True refuses if
    # tasks are running; we scale first to make that a non-issue.
    ecs_client.update_service(
        cluster=cluster, service=service_name, desiredCount=0,
    )
    ecs_client.delete_service(
        cluster=cluster, service=service_name, force=True,
    )
    logger.info("supervisor.service_deleted service=%s", service_name)


def reconcile(
    *,
    ecs_client: Any,
    decision: Decision,
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
) -> None:
    """Make the decision true in ECS."""

    if decision.action is Action.NOOP:
        return
    if decision.strategy_id is None:
        logger.warning("supervisor.decision_without_strategy_id action=%s",
                       decision.action.value)
        return

    if decision.action is Action.ENSURE_RUNNING:
        _ensure_running(
            ecs_client,
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
    elif decision.action is Action.PAUSE:
        _pause(ecs_client, cluster, decision.strategy_id)
    elif decision.action is Action.STOP:
        _stop(ecs_client, cluster, decision.strategy_id)


# ── Lambda glue ─────────────────────────────────────────────────────


def _run(
    *,
    ecs_client: Any,
    event: dict[str, Any],
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
) -> dict[str, Any]:
    """Process one DDB Streams batch. Per-record errors are caught so
    a single bad record doesn't force a full-batch replay."""

    records = event.get("Records", [])
    action_counts: dict[str, int] = {}
    errors = 0
    for rec in records:
        try:
            decision = classify_record(rec)
            action_counts[decision.action.value] = (
                action_counts.get(decision.action.value, 0) + 1
            )
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
        except Exception:
            errors += 1
            logger.exception("supervisor.record_failed")
    summary = {
        "processed": len(records),
        "errors": errors,
        "actions": action_counts,
    }
    logger.info(
        "supervisor.batch_complete processed=%d errors=%d",
        len(records), errors,
    )
    return summary


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. Driven by DynamoDB Streams.

    Env vars (all required):
        ECS_CLUSTER
        TASK_DEFINITION_FAMILY
        CONTAINER_IMAGE
        TASK_ROLE_ARN
        EXECUTION_ROLE_ARN
        SUBNET_IDS          — comma-separated
        SECURITY_GROUP_IDS  — comma-separated
        LOG_GROUP_NAME
        AWS_REGION          — set by Lambda runtime
        DYNAMODB_TABLE      — passed through to the bot's env
    """

    import boto3

    ecs = boto3.client("ecs")
    extra_env = {
        "DYNAMODB_TABLE": os.environ["DYNAMODB_TABLE"],
    }
    # Optional pass-throughs: the bot boots faster if it doesn't have
    # to re-fetch things that are constant. Only forward what's set.
    for k in ("AGENT_MEMORY_BUCKET", "SECRETS_MANAGER_SECRET_NAME"):
        v = os.environ.get(k)
        if v:
            extra_env[k] = v

    return _run(
        ecs_client=ecs,
        event=event,
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
    )
