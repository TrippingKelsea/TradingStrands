"""Helper: record token usage from a Strands Agent result.

Strands exposes accumulated token usage via `result.metrics.accumulated_usage`
as {inputTokens, outputTokens, totalTokens}. This module extracts those and
writes a TokenUsage record to DynamoDB.

Usage:
    from trading_strands.token_telemetry.record import record_from_result

    result = await agent.invoke_async(prompt, ...)
    record_from_result(
        store=token_store,
        result=result,
        org_id="...",
        agent_id="...",
        agent_type="strategy",
        model="claude-sonnet-4-6",
    )

Missing / zero tokens → the record is skipped entirely (we don't emit
zero-usage events, they're noise). Failures during recording are swallowed
so a DDB outage doesn't break the trading loop.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from trading_strands.token_telemetry.store import (
    TokenUsage,
    TokenUsageStore,
    compute_cost,
)

logger = structlog.get_logger()


def record_from_result(
    store: TokenUsageStore | None,
    result: Any,
    org_id: str,
    agent_id: str,
    agent_type: str,
    model: str,
) -> None:
    """Extract token usage from a Strands AgentResult and persist.

    No-op if `store` is None (the trading service may run without
    telemetry configured, e.g., in unit tests or local-dev mode).
    """

    if store is None:
        return

    try:
        # CRITICAL: read per-invocation usage, NOT result.metrics.
        # accumulated_usage. The accumulated field is per-Agent
        # lifetime cumulative; using it causes each tick to re-record
        # the running total, so N ticks record ~N² total tokens.
        # Observed bug: Dumb Trader showed 420B input tokens across
        # 3.5K ticks (~120M/tick, larger than Claude's 200K context).
        # The fix is `latest_agent_invocation.usage` — the most recent
        # invocation only.
        metrics = getattr(result, "metrics", None)
        if metrics is None:
            return
        invocation = getattr(metrics, "latest_agent_invocation", None)
        usage_obj = getattr(invocation, "usage", None) if invocation else None
        if usage_obj is None:
            # Fall-back path for older Strands versions that don't
            # expose latest_agent_invocation. Better to record zero
            # and alarm on missing data than to silently over-count.
            return
        # Strands uses a dict-like shape with camelCase keys.
        if isinstance(usage_obj, dict):
            input_t = int(usage_obj.get("inputTokens", 0))
            output_t = int(usage_obj.get("outputTokens", 0))
        else:
            input_t = int(getattr(usage_obj, "inputTokens", 0))
            output_t = int(getattr(usage_obj, "outputTokens", 0))
        if input_t == 0 and output_t == 0:
            return

        usage = TokenUsage(
            org_id=org_id,
            agent_id=agent_id,
            agent_type=agent_type,
            model=model,
            input_tokens=input_t,
            output_tokens=output_t,
            cost_usd_est=compute_cost(model, input_t, output_t),
            timestamp=int(time.time()),
        )
        store.record(usage)
    except Exception:
        logger.exception(
            "token_telemetry.record_failed",
            agent_id=agent_id, agent_type=agent_type,
        )
