"""BotProvisioner Lambda: weekend fan-out driver.

Triggered by EventBridge on the weekend schedule. Enumerates active
Strategy Agents across all orgs, invokes the Self-Critique Lambda once
per bot (async / InvocationType=Event), and returns a summary dict.

Design notes:

- Async invocation is deliberate. One slow reflection must not block
  the rest of the fleet, and the schedule doesn't care whether the
  critique takes 30 seconds or 5 minutes.
- Per-bot errors are caught and reported in the summary rather than
  propagated — the provisioner's contract is "I tried everyone", not
  "I succeeded for everyone". A single Lambda invoke failing is also
  qualitatively different from the reflection itself failing, which
  the Self-Critique Lambda handles internally.
- Enumeration uses StrategyStore.list_all() — this is a sysadmin-level
  read, intentional: the provisioner is an infrastructure component,
  not a user-facing request.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import structlog

from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()


@dataclass
class InvocationResult:
    """One Lambda invocation outcome."""

    org_id: str
    bot_id: str
    ok: bool
    error: str | None = None


def enumerate_active_bots(store: StrategyStore) -> list[tuple[str, str]]:
    """Return (org_id, bot_id) pairs for every ACTIVE strategy.

    bot_id follows the `strategy-{strategy_id}` convention used
    throughout the codebase (see app.py registration and
    lambda_handler._load_strategy_prompt). Keeping that convention
    centralized here means the Self-Critique Lambda keeps its single
    reverse-mapping rule and nothing else needs to know.
    """

    strategies = store.list_all()
    return [
        (s.org_id, f"strategy-{s.strategy_id}")
        for s in strategies
        if s.status == StrategyStatus.ACTIVE
    ]


def fan_out_self_critique(
    *,
    lambda_client: Any,
    function_name: str,
    bots: list[tuple[str, str]],
) -> list[InvocationResult]:
    """Invoke the Self-Critique Lambda once per bot, asynchronously.

    Returns one InvocationResult per input bot, preserving order.
    """

    results: list[InvocationResult] = []
    for org_id, bot_id in bots:
        payload = json.dumps({"org_id": org_id, "bot_id": bot_id}).encode("utf-8")
        try:
            lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=payload,
            )
        except Exception as exc:
            results.append(InvocationResult(
                org_id=org_id, bot_id=bot_id, ok=False, error=str(exc),
            ))
            continue
        results.append(InvocationResult(org_id=org_id, bot_id=bot_id, ok=True))
    return results


def _run(
    *,
    table: Any,
    lambda_client: Any,
    self_critique_function_name: str,
) -> dict[str, Any]:
    """Do the work. Split out from `handler` so tests can inject fakes
    without mocking boto3 at module level."""

    store = StrategyStore(table)
    bots = enumerate_active_bots(store)
    results = fan_out_self_critique(
        lambda_client=lambda_client,
        function_name=self_critique_function_name,
        bots=bots,
    )
    succeeded = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    summary: dict[str, Any] = {
        "ok": failed == 0,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "errors": [
            {"bot_id": r.bot_id, "error": r.error}
            for r in results if not r.ok
        ],
    }
    logger.info(
        "bot_provisioner.complete total=%d succeeded=%d failed=%d",
        len(results), succeeded, failed,
    )
    return summary


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. EventBridge passes no meaningful payload on a
    scheduled invocation — we enumerate from DDB ourselves.

    Env vars:
        DYNAMODB_TABLE             — strategies table
        SELF_CRITIQUE_FUNCTION_NAME — Self-Critique Lambda name
    """

    import boto3

    ddb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda")
    table_name = os.environ["DYNAMODB_TABLE"]
    fn_name = os.environ["SELF_CRITIQUE_FUNCTION_NAME"]
    return _run(
        table=ddb.Table(table_name),
        lambda_client=lambda_client,
        self_critique_function_name=fn_name,
    )
