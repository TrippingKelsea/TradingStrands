"""Auditor Agent Lambda entry point.

Event: {"org_id": "..."}. Uses the org's Alpaca credentials (same
secret the trading service already reads) to pull broker positions,
aggregates ledgers across the org's active bots, runs the
deterministic reconciliation, and calls the runner.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.alpaca_secrets.store import secret_name_for
from trading_strands.auditor_agent.runner import run_audit_review
from trading_strands.broker.alpaca import AlpacaAdapter
from trading_strands.ledger_store.store import LedgerStore
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()


class _DdbHaltControl:
    """Per-org halt writer. Scope is intentional: the Auditor Agent
    that invoked this handler only reviewed ONE org's ledger vs broker.
    If it saw drift, halting only that org keeps sibling orgs running.
    System-wide halt stays the sysadmin's lever via the dashboard."""

    def __init__(self, table: Any, org_id: str) -> None:
        self._table = table
        self._org_id = org_id

    def set_halted(self, halted: bool, reason: str = "") -> None:
        from trading_strands.halt.store import HaltStore

        HaltStore(self._table).set_org_halt(
            self._org_id, halted, reason=reason,
        )


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


def _load_org_broker(
    sm_client: Any, org_id: str,
) -> AlpacaAdapter:
    """Read the org's Alpaca creds and build a broker adapter. Same
    path layout the trading service uses."""

    secret_name = secret_name_for(org_id)
    resp = sm_client.get_secret_value(SecretId=secret_name)
    payload = json.loads(resp.get("SecretString", "{}"))
    return AlpacaAdapter(
        api_key=payload.get("ALPACA_API_KEY", ""),
        secret_key=payload.get("ALPACA_SECRET_KEY", ""),
        paper=str(payload.get("ALPACA_PAPER", "true")).lower() == "true",
    )


def _load_org_ledgers(
    table: Any, ledger_store: LedgerStore, org_id: str,
) -> dict[str, Any]:
    strategies = StrategyStore(table).list_for_org(org_id)
    active = [s for s in strategies if s.status == StrategyStatus.ACTIVE]
    out: dict[str, Any] = {}
    for strat in active:
        bot_id = f"strategy-{strat.strategy_id}"
        ledger = ledger_store.load_snapshot(bot_id)
        if ledger is not None:
            out[bot_id] = ledger
    return out


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    org_id = event.get("org_id")
    if not org_id:
        return {"ok": False, "error": "missing org_id"}

    import anyio
    import boto3

    table_name = os.environ["DYNAMODB_TABLE"]
    bucket = os.environ["AGENT_MEMORY_BUCKET"]
    model_id = os.environ.get(
        "AUDITOR_AGENT_MODEL_ID", "us.anthropic.claude-sonnet-4-6",
    )

    ddb = boto3.resource("dynamodb")
    s3 = boto3.client("s3")
    sm = boto3.client("secretsmanager")
    table = ddb.Table(table_name)
    ledger_store = LedgerStore(table)

    memory = AgentMemoryStore(
        s3_client=s3, bucket=bucket,
        org_id=org_id, agent_type="auditor", agent_id=org_id,
    )

    halt_control = _DdbHaltControl(table, org_id)
    ledgers = _load_org_ledgers(table, ledger_store, org_id)

    try:
        broker = _load_org_broker(sm, org_id)
        broker_positions = anyio.run(broker.get_positions)
    except Exception as exc:
        # Couldn't fetch broker state — record the problem as a drift
        # event of its own. A silent failure here would be worse than
        # a vocal one: an auditor that can't see broker state is an
        # auditor that can't do its job.
        logger.exception("auditor.broker_fetch_failed org_id=%s", org_id)
        return {
            "ok": False,
            "org_id": org_id,
            "error": f"broker_fetch: {exc}",
        }

    report = run_audit_review(
        org_id=org_id,
        memory_store=memory,
        halt_control=halt_control,
        ledgers=ledgers,
        broker_positions=broker_positions,
        llm_invoker=_strands_invoker(model_id),
    )
    return {
        "ok": not report.errors and report.check_status == "pass",
        "org_id": report.org_id,
        "date": report.date,
        "check_status": report.check_status,
        "halt_triggered": report.halt_triggered,
        "recommendation_length": len(report.recommendation),
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        "errors": report.errors,
    }
