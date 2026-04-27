"""Risk Agent Lambda entry point.

Invoked per-org by EventBridge on a weekly cadence. The BotProvisioner
(or a sibling Lambda) enumerates orgs and invokes this function once
each — same fan-out pattern as Self-Critique per bot, but scoped at
the org level since risk reasoning is fleet-wide.

Event shape:
    {"org_id": "..."}

Env:
    DYNAMODB_TABLE
    AGENT_MEMORY_BUCKET
    RISK_AGENT_MODEL_ID   (optional; defaults to Sonnet)
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.ledger_store.store import LedgerStore
from trading_strands.risk_agent.runner import run_risk_review
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()


def _strands_invoker(model_id: str) -> Any:
    """Build the Strands Agent-backed (system, user) -> (text, tokens_in,
    tokens_out) adapter. Lazy import so tests don't drag in Bedrock."""

    import anyio
    from strands import Agent

    def _invoke(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        agent = Agent(model=model_id, system_prompt=system_prompt)
        result = anyio.run(agent.invoke_async, user_prompt)
        text = str(result)
        usage = getattr(
            getattr(result, "metrics", None), "accumulated_usage", None,
        )
        if isinstance(usage, dict):
            return (
                text,
                int(usage.get("inputTokens", 0)),
                int(usage.get("outputTokens", 0)),
            )
        tokens_in = int(getattr(usage, "inputTokens", 0)) if usage else 0
        tokens_out = int(getattr(usage, "outputTokens", 0)) if usage else 0
        return text, tokens_in, tokens_out

    return _invoke


def _load_org_ledgers(
    table: Any, ledger_store: LedgerStore, org_id: str,
) -> dict[str, Any]:
    """Load the current ledger snapshot for every active strategy in
    the org. Bots with no snapshot yet are skipped — a fresh bot has
    no risk to review."""

    strategies = StrategyStore(table).list_for_org(org_id)
    active = [s for s in strategies if s.status == StrategyStatus.ACTIVE]
    ledgers: dict[str, Any] = {}
    for strat in active:
        bot_id = f"strategy-{strat.strategy_id}"
        ledger = ledger_store.load_snapshot(bot_id)
        if ledger is not None:
            ledgers[bot_id] = ledger
    return ledgers


def _load_org_recent_fills(
    ledger_store: LedgerStore, org_id: str, table: Any,
    per_bot_limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent fills across every active bot in the org, newest
    first. Per-bot limit caps the scan; aggregate list is merged + re-
    sorted by timestamp."""

    strategies = StrategyStore(table).list_for_org(org_id)
    active = [s for s in strategies if s.status == StrategyStatus.ACTIVE]
    out: list[dict[str, Any]] = []
    for strat in active:
        bot_id = f"strategy-{strat.strategy_id}"
        try:
            out.extend(
                ledger_store.events_for_bot(bot_id, limit=per_bot_limit),
            )
        except Exception:
            logger.exception("risk_agent.events_for_bot_failed bot_id=%s", bot_id)
    out.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)
    return out


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. Returns a JSON-serializable summary.

    Errors are caught and returned in the response — the invoker (a
    weekly EventBridge rule) doesn't benefit from Lambda retry.
    """

    org_id = event.get("org_id")
    if not org_id:
        return {"ok": False, "error": "missing org_id"}

    import boto3

    table_name = os.environ["DYNAMODB_TABLE"]
    bucket = os.environ["AGENT_MEMORY_BUCKET"]
    model_id = os.environ.get("RISK_AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    ddb = boto3.resource("dynamodb")
    s3 = boto3.client("s3")
    table = ddb.Table(table_name)
    ledger_store = LedgerStore(table)

    # Heartbeat (untracked-cadence). Platform Supervisor records the beat
    # but doesn't drive alarms off it — the weekly cadence is too slow
    # to classify against tick-level thresholds. Still valuable: the
    # dashboard can show "last risk review: 3 days ago" per org.
    import contextlib as _contextlib

    from trading_strands.heartbeat.store import HeartbeatStore as _HB
    with _contextlib.suppress(Exception):
        _HB(table).beat(agent_type="risk", agent_id=org_id)

    memory = AgentMemoryStore(
        s3_client=s3, bucket=bucket,
        org_id=org_id, agent_type="risk", agent_id=org_id,
    )

    ledgers = _load_org_ledgers(table, ledger_store, org_id)
    recent_fills = _load_org_recent_fills(ledger_store, org_id, table)

    report = run_risk_review(
        org_id=org_id,
        memory_store=memory,
        ledgers=ledgers,
        recent_fills=recent_fills,
        llm_invoker=_strands_invoker(model_id),
    )
    return {
        "ok": not report.errors,
        "org_id": report.org_id,
        "date": report.date,
        "recommendation_length": len(report.recommendation),
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        "context_bytes": report.context_bytes,
        "bot_count": len(ledgers),
        "errors": report.errors,
    }
