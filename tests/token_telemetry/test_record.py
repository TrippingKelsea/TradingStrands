"""Regression tests for record_from_result.

The bug: record_from_result was reading result.metrics.accumulated_usage,
which Strands documents as "Accumulated token usage across all model
invocations (across all requests)" — i.e., cumulative over the Agent's
lifetime, not per-invocation. Recording that field once per tick
causes the DDB counter to grow as ~N^2 in tokens per N ticks.

Observed in production: one bot showed 420B input tokens across 3.5K
ticks — ~120M/tick, larger than Claude's 200K context window. The
fix reads latest_agent_invocation.usage (per-call) instead.

These tests pin the new behavior so a future refactor can't
accidentally revert.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from trading_strands.token_telemetry.record import record_from_result


def _result_with(input_t: int, output_t: int) -> SimpleNamespace:
    """Build a fake Strands AgentResult with both the accumulated
    field (what we WERE reading, wrongly) and the per-invocation
    field (what we read now). The per-invocation value is the
    truth; accumulated is double what it should be to catch any
    regression that reverts to reading the wrong field."""

    per_call_usage = {"inputTokens": input_t, "outputTokens": output_t}
    accumulated = {"inputTokens": input_t * 99, "outputTokens": output_t * 99}
    invocation = SimpleNamespace(usage=per_call_usage)
    metrics = SimpleNamespace(
        accumulated_usage=accumulated,
        latest_agent_invocation=invocation,
    )
    return SimpleNamespace(metrics=metrics)


def test_records_per_invocation_usage_not_accumulated() -> None:
    """The primary regression test: per-call usage is what lands
    in the store, not the accumulated lifetime total."""

    store = MagicMock()
    record_from_result(
        store=store,
        result=_result_with(input_t=1000, output_t=200),
        org_id="org-1",
        agent_id="bot-1",
        agent_type="strategy",
        model="us.anthropic.claude-sonnet-4-6",
    )
    store.record.assert_called_once()
    usage = store.record.call_args.args[0]
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200


def test_noop_when_usage_is_zero() -> None:
    """Zero-usage events are noise. A run that completed but
    somehow reported zero tokens shouldn't clutter the counter."""

    store = MagicMock()
    record_from_result(
        store=store,
        result=_result_with(input_t=0, output_t=0),
        org_id="org-1", agent_id="bot-1",
        agent_type="strategy", model="us.anthropic.claude-sonnet-4-6",
    )
    store.record.assert_not_called()


def test_noop_when_store_is_none() -> None:
    """Local dev / test-only callers pass store=None."""

    record_from_result(
        store=None,
        result=_result_with(1, 1),
        org_id="o", agent_id="a", agent_type="strategy", model="m",
    )


def test_noop_when_no_latest_invocation() -> None:
    """Older Strands versions (or pre-invocation failure paths)
    may not expose latest_agent_invocation. Skip cleanly rather
    than falling back to the accumulated-usage bug."""

    store = MagicMock()
    result = SimpleNamespace(metrics=SimpleNamespace(
        accumulated_usage={"inputTokens": 5000, "outputTokens": 1000},
        latest_agent_invocation=None,
    ))
    record_from_result(
        store=store,
        result=result,
        org_id="o", agent_id="a", agent_type="strategy", model="m",
    )
    store.record.assert_not_called()


def test_handles_object_attrs_not_dict() -> None:
    """Some Strands versions return usage as an object with
    inputTokens/outputTokens attrs instead of a dict."""

    store = MagicMock()
    invocation = SimpleNamespace(
        usage=SimpleNamespace(inputTokens=42, outputTokens=7),
    )
    result = SimpleNamespace(metrics=SimpleNamespace(
        latest_agent_invocation=invocation,
    ))
    record_from_result(
        store=store, result=result,
        org_id="o", agent_id="a", agent_type="strategy",
        model="us.anthropic.claude-sonnet-4-6",
    )
    store.record.assert_called_once()
    usage = store.record.call_args.args[0]
    assert usage.input_tokens == 42
    assert usage.output_tokens == 7
