"""Tests for tool-call EMF emission helpers."""

from __future__ import annotations

import json

import pytest

from trading_strands.tools.observability import (
    emit_tool_outcome,
    tool_call_timer,
)


def _parse_emitted(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    out = capsys.readouterr().out.strip()
    if not out:
        return []
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_emit_outcome_count_with_dimensions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_tool_outcome(
        tool="news", outcome="success",
        strategy_id="strat-1", org_id="org-a",
    )
    records = _parse_emitted(capsys)
    assert len(records) == 1
    r = records[0]
    assert r["tool.call.count"] == 1
    assert r["tool"] == "news"
    assert r["outcome"] == "success"
    # Extras ride as searchable fields, not dimensions.
    assert r["strategy_id"] == "strat-1"
    assert r["org_id"] == "org-a"


def test_emit_outcome_includes_symbol_when_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_tool_outcome(
        tool="news", outcome="cache_hit",
        strategy_id="s", org_id="o", symbol="AAPL",
    )
    records = _parse_emitted(capsys)
    assert records[0]["symbol"] == "AAPL"


def test_emit_outcome_quota_exceeded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """quota_exceeded is the specific outcome CloudWatch alarms key
    off — must land in the metric payload as the dimension value."""

    emit_tool_outcome(
        tool="news", outcome="quota_exceeded",
        strategy_id="s", org_id="o",
    )
    records = _parse_emitted(capsys)
    assert records[0]["outcome"] == "quota_exceeded"


def test_timer_emits_latency_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with tool_call_timer(
        "news", strategy_id="s", org_id="o", symbol="AAPL",
    ):
        pass   # no-op
    records = _parse_emitted(capsys)
    assert len(records) == 1
    assert "tool.call.latency_ms" in records[0]
    assert records[0]["tool"] == "news"
    assert records[0]["symbol"] == "AAPL"
    # Latency is non-negative.
    assert records[0]["tool.call.latency_ms"] >= 0


def test_timer_emits_on_exception_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A slow failing call is different from a fast failing call —
    operators want both latency data points."""

    with pytest.raises(RuntimeError), tool_call_timer(
        "news", strategy_id="s", org_id="o",
    ):
        raise RuntimeError("boom")
    records = _parse_emitted(capsys)
    assert len(records) == 1
    assert "tool.call.latency_ms" in records[0]
