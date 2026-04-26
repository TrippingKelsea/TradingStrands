"""Tests for the EMF emitter.

Captures stdout and parses the JSON line to verify the shape CloudWatch
Logs expects. Also ensures dimensions are required (the most common way
to emit a meaningless metric).
"""

from __future__ import annotations

import json
import time

import pytest

from trading_strands.emf.emitter import (
    DEFAULT_NAMESPACE,
    emit_metric,
    timed_metric,
)


def _captured_emf(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    assert out, "expected an EMF line on stdout"
    # There might be multiple lines; return the last (most recent).
    lines = [line for line in out.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_emit_metric_writes_emf_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    emit_metric(
        name="test.count",
        value=42,
        unit="Count",
        dimensions={"service": "strategy", "org_id": "org-a"},
    )
    record = _captured_emf(capsys)

    assert "_aws" in record
    aws = record["_aws"]
    assert "Timestamp" in aws
    cw = aws["CloudWatchMetrics"][0]
    assert cw["Namespace"] == DEFAULT_NAMESPACE
    assert cw["Dimensions"] == [["service", "org_id"]]
    assert cw["Metrics"] == [{"Name": "test.count", "Unit": "Count"}]
    # Dimension values are present as top-level fields so CloudWatch
    # can index them.
    assert record["service"] == "strategy"
    assert record["org_id"] == "org-a"
    # Metric value is present as the top-level metric-name field.
    assert record["test.count"] == 42


def test_emit_metric_dimension_values_coerced_to_str(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_metric(
        name="test.count", value=1, unit="Count",
        dimensions={"count": 99, "enabled": True},  # type: ignore[dict-item]
    )
    record = _captured_emf(capsys)
    assert record["count"] == "99"
    assert record["enabled"] == "True"


def test_emit_metric_rejects_empty_dimensions() -> None:
    with pytest.raises(ValueError, match="at least one dimension"):
        emit_metric(name="x", value=1, unit="Count", dimensions={})


def test_emit_metric_with_extra_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_metric(
        name="test.count", value=1, unit="Count",
        dimensions={"svc": "x"},
        extra={"trace_id": "abc123", "error": "timeout"},
    )
    record = _captured_emf(capsys)
    assert record["trace_id"] == "abc123"
    assert record["error"] == "timeout"


def test_emit_metric_extra_cannot_shadow_metric_or_dimensions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defensive: if a caller passes the same key as a dimension or metric
    in `extra`, the emitted record keeps the metric/dimension value."""

    emit_metric(
        name="test.count", value=42, unit="Count",
        dimensions={"svc": "right"},
        extra={"svc": "wrong", "test.count": 999},
    )
    record = _captured_emf(capsys)
    assert record["svc"] == "right"
    assert record["test.count"] == 42


def test_timed_metric_emits_milliseconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with timed_metric("op.latency_ms", {"op": "sleep"}):
        time.sleep(0.02)
    record = _captured_emf(capsys)
    assert record["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Milliseconds"
    # Observed latency should be at least 20ms.
    assert record["op.latency_ms"] >= 20


def test_timed_metric_emits_on_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exceptions inside the timed block still produce a metric — a slow
    failure is different from a fast one and both are interesting."""

    with pytest.raises(RuntimeError), timed_metric("op.latency_ms", {"op": "broken"}):
        raise RuntimeError("boom")
    record = _captured_emf(capsys)
    assert record["op"] == "broken"
