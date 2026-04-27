"""Compliance Agent Lambda entry point.

Event: {"org_id": "..."}. Enumerates the org's active strategies,
builds a StrategyMandate per strategy, calls the runner.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.compliance_agent.runner import (
    StrategyMandate,
    run_compliance_review,
)
from trading_strands.ledger_store.store import LedgerStore
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()


def _strands_invoker(model_id: str) -> Any:
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


def _build_mandates(
    table: Any, ledger_store: LedgerStore, org_id: str,
    per_bot_fills: int = 15,
) -> list[StrategyMandate]:
    """Build a StrategyMandate per active strategy in the org."""

    strategies = StrategyStore(table).list_for_org(org_id)
    out: list[StrategyMandate] = []
    for strat in strategies:
        if strat.status != StrategyStatus.ACTIVE:
            continue
        bot_id = f"strategy-{strat.strategy_id}"
        ledger = ledger_store.load_snapshot(bot_id)
        try:
            fills = ledger_store.events_for_bot(bot_id, limit=per_bot_fills)
        except Exception:
            logger.exception("compliance.events_failed bot_id=%s", bot_id)
            fills = []
        out.append(StrategyMandate(
            strategy_id=strat.strategy_id,
            bot_id=bot_id,
            name=strat.name,
            prompt_markdown=strat.markdown,
            declared_symbols=list(strat.symbols),
            ledger=ledger,
            recent_fills=fills,
        ))
    return out


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    org_id = event.get("org_id")
    if not org_id:
        return {"ok": False, "error": "missing org_id"}

    import boto3

    table_name = os.environ["DYNAMODB_TABLE"]
    bucket = os.environ["AGENT_MEMORY_BUCKET"]
    model_id = os.environ.get(
        "COMPLIANCE_AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6",
    )

    ddb = boto3.resource("dynamodb")
    s3 = boto3.client("s3")
    table = ddb.Table(table_name)
    ledger_store = LedgerStore(table)

    import contextlib as _contextlib

    from trading_strands.heartbeat.store import HeartbeatStore as _HB
    with _contextlib.suppress(Exception):
        _HB(table).beat(agent_type="compliance", agent_id=org_id)

    memory = AgentMemoryStore(
        s3_client=s3, bucket=bucket,
        org_id=org_id, agent_type="compliance", agent_id=org_id,
    )

    strategies = _build_mandates(table, ledger_store, org_id)

    report = run_compliance_review(
        org_id=org_id,
        memory_store=memory,
        strategies=strategies,
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
        "strategy_count": len(strategies),
        "skipped_reason": report.skipped_reason,
        "errors": report.errors,
    }
