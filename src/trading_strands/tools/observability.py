"""EMF-emission helpers for tool calls.

Every tool call emits one `tool.call.count` metric with an `outcome`
dimension, plus a `tool.call.latency_ms` metric for calls that
actually reached the external API. Dashboards and CloudWatch alarms
key off these.

Design notes:
- Helper rather than a decorator so tools can decide exactly what
  they time (some wrap only the HTTP call; others include the
  cache-read + translate).
- strategy_id + org_id ride as `extra` (searchable in Logs Insights)
  rather than dimensions — keeps CloudWatch cardinality bounded.
  Dimensions are the alarm-relevant (tool, outcome); extras are
  forensic.

Keep additions to this module tight. Every dimension we add multiplies
cost across the fleet; every bit of non-structured context goes into
extras instead.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import Any

from trading_strands.emf.emitter import emit_metric


def emit_tool_outcome(
    tool: str,
    outcome: str,
    *,
    strategy_id: str,
    org_id: str,
    symbol: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a tool.call.count=1 for one (tool, outcome) pair.

    Valid outcomes:
      - "success"        — external call returned a result
      - "cache_hit"      — served entirely from cache
      - "quota_exceeded" — rejected before call; no external work
      - "error"          — external call raised
    """

    full_extra: dict[str, Any] = {"strategy_id": strategy_id, "org_id": org_id}
    if symbol:
        full_extra["symbol"] = symbol
    if extra:
        full_extra.update(extra)
    emit_metric(
        "tool.call.count",
        value=1,
        unit="Count",
        dimensions={"tool": tool, "outcome": outcome},
        extra=full_extra,
    )


@contextlib.contextmanager
def tool_call_timer(
    tool: str,
    *,
    strategy_id: str,
    org_id: str,
    symbol: str | None = None,
) -> Iterator[None]:
    """Time an external tool call. Emits tool.call.latency_ms on
    context exit — on both success AND failure paths, because a
    slow failing call is different from a fast failing call and
    operators want to see both.

    The outcome metric is emitted separately via emit_tool_outcome
    — this timer only measures latency.
    """

    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        full_extra: dict[str, Any] = {
            "strategy_id": strategy_id, "org_id": org_id,
        }
        if symbol:
            full_extra["symbol"] = symbol
        emit_metric(
            "tool.call.latency_ms",
            value=elapsed_ms,
            unit="Milliseconds",
            dimensions={"tool": tool},
            extra=full_extra,
        )
