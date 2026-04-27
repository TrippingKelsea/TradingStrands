"""AWS Lambda entry point for the Self-Critique Agent.

Invoked on the weekend schedule (EventBridge cron). One invocation per
active Strategy Agent; the invoker passes (org_id, bot_id) in the event.

Event shape:
    {
      "org_id":   "...",
      "bot_id":   "...",
      "strategy_prompt": "..." | null   # if null, loaded from DynamoDB
    }

Env vars expected (configured by CDK):
    DYNAMODB_TABLE          — source of strategy prompts + ledger snapshots
    AGENT_MEMORY_BUCKET     — S3 bucket holding agent memory files
    SELF_CRITIQUE_MODEL_ID  — Bedrock model ID for reflections

The Lambda itself is thin: load the strategy, build a Strands agent,
construct the adapters, call run_self_critique, return a JSON summary.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.ledger_store.store import LedgerStore
from trading_strands.self_critique.runner import run_self_critique

logger = structlog.get_logger()


def _strands_invoker(model_id: str) -> Any:
    """Build the (system, user) -> (text, tokens_in, tokens_out) adapter
    backed by a Strands Agent.

    Imported lazily so unit tests don't pull in strands (+ its transitive
    bedrock runtime client). Tests use a canned stub at the runner layer.
    """

    import anyio
    from strands import Agent

    def _invoke(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        agent = Agent(model=model_id, system_prompt=system_prompt)
        # Strands exposes invoke_async; drive it sync here since Lambda
        # handlers are sync by default.
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


def _load_strategy_prompt(table: Any, bot_id: str) -> str:
    """Fetch the strategy markdown from DDB using the strategy store."""

    strategy_id = bot_id.removeprefix("strategy-")
    resp = table.get_item(Key={"pk": f"STRATEGY#{strategy_id}"})
    item = resp.get("Item") or {}
    return str(item.get("markdown", ""))


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. Returns a JSON-serializable summary dict.

    Errors are caught and returned in the response — EventBridge doesn't
    benefit from Lambda re-invocation on error (next invocation is next
    weekend regardless). The response is logged for operator review.
    """

    org_id = event.get("org_id")
    bot_id = event.get("bot_id")
    if not org_id or not bot_id:
        return {"ok": False, "error": "missing org_id or bot_id"}

    import boto3

    ddb = boto3.resource("dynamodb")
    s3 = boto3.client("s3")
    table_name = os.environ["DYNAMODB_TABLE"]
    table = ddb.Table(table_name)

    # Heartbeat keyed by the bot being critiqued — one row per
    # bot per reflection cadence. Helps answer "did Saturday's
    # self-critique actually run for strategy-abc?"
    import contextlib as _contextlib

    from trading_strands.heartbeat.store import HeartbeatStore as _HB
    with _contextlib.suppress(Exception):
        _HB(table).beat(agent_type="self_critique", agent_id=bot_id)

    memory_bucket = os.environ["AGENT_MEMORY_BUCKET"]
    model_id = os.environ.get("SELF_CRITIQUE_MODEL_ID", "")

    strategy_prompt = event.get("strategy_prompt")
    if not strategy_prompt:
        strategy_prompt = _load_strategy_prompt(table, bot_id)
    if not strategy_prompt:
        return {"ok": False, "error": f"no strategy prompt for bot {bot_id}"}

    memory = AgentMemoryStore(
        s3_client=s3, bucket=memory_bucket,
        org_id=org_id, agent_type="strategy", agent_id=bot_id,
    )
    ledger_store = LedgerStore(table)
    ledger = ledger_store.load_snapshot(bot_id)

    report = run_self_critique(
        bot_id=bot_id,
        strategy_prompt=strategy_prompt,
        memory_store=memory,
        ledger=ledger,
        llm_invoker=_strands_invoker(model_id),
    )

    return {
        "ok": not report.errors,
        "bot_id": report.bot_id,
        "date": report.date,
        "reflection_length": len(report.reflection),
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        "context_bytes": report.context_bytes,
        "errors": report.errors,
    }
