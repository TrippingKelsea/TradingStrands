"""Org-fanout Lambda.

Walks TenancyStore.list_orgs() and invokes the target review-agent
function once per org via async (Event) Lambda invoke. The same
shape as BotProvisioner; kept in its own module because the two
expand along different axes (BotProvisioner over strategies, this
one over orgs) and co-locating them would invite future confusion.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import structlog

from trading_strands.tenancy.store import TenancyStore

logger = structlog.get_logger()


@dataclass
class InvocationResult:
    org_id: str
    ok: bool
    error: str | None = None


def enumerate_orgs(store: TenancyStore) -> list[str]:
    """Every org's id. Includes the system org — sysadmin-run
    strategies exist there and need the same review coverage as
    customer-org strategies."""

    return [o.org_id for o in store.list_orgs()]


def fan_out_review(
    *,
    lambda_client: Any,
    target_function: str,
    org_ids: list[str],
) -> list[InvocationResult]:
    """Invoke `target_function` once per org, asynchronously.

    Order of results matches order of input; per-org errors are caught
    and reported on the result, not raised.
    """

    results: list[InvocationResult] = []
    for org_id in org_ids:
        payload = json.dumps({"org_id": org_id}).encode("utf-8")
        try:
            lambda_client.invoke(
                FunctionName=target_function,
                InvocationType="Event",
                Payload=payload,
            )
        except Exception as exc:
            results.append(InvocationResult(
                org_id=org_id, ok=False, error=str(exc),
            ))
            continue
        results.append(InvocationResult(org_id=org_id, ok=True))
    return results


def _run(
    *,
    store: TenancyStore,
    lambda_client: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Do the work. Split out from `handler` for test injection."""

    target = event.get("target_function")
    if not target:
        return {
            "ok": False,
            "error": "missing target_function in event payload",
        }
    org_ids = enumerate_orgs(store)
    results = fan_out_review(
        lambda_client=lambda_client,
        target_function=str(target),
        org_ids=org_ids,
    )
    succeeded = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    summary = {
        "ok": failed == 0,
        "target": target,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "errors": [
            {"org_id": r.org_id, "error": r.error}
            for r in results if not r.ok
        ],
    }
    logger.info(
        "org_fanout.complete target=%s total=%d succeeded=%d failed=%d",
        target, len(results), succeeded, failed,
    )
    return summary


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. EventBridge rules pass the target function
    in the event payload; see docs/SPEC/agents.md for the cadence.

    Env:
        DYNAMODB_TABLE — tenancy table (same table used by everything)
    """

    import boto3

    ddb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda")
    table = ddb.Table(os.environ["DYNAMODB_TABLE"])
    store = TenancyStore(table)
    return _run(store=store, lambda_client=lambda_client, event=event)
