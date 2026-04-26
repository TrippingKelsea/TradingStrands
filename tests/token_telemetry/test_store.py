"""Tests for token usage telemetry."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from trading_strands.token_telemetry.store import (
    TokenUsage,
    TokenUsageStore,
    _date_key,
    compute_cost,
)


def _usage(
    org: str = "org-a", agent: str = "bot-1",
    input_t: int = 100, output_t: int = 50,
    cost: str = "0.01", ts: int | None = None,
) -> TokenUsage:
    return TokenUsage(
        org_id=org, agent_id=agent, agent_type="strategy",
        model="claude-sonnet-4-6",
        input_tokens=input_t, output_tokens=output_t,
        cost_usd_est=Decimal(cost),
        timestamp=ts if ts is not None else int(time.time()),
    )


def test_compute_cost_known_model() -> None:
    # claude-sonnet-4-6: $0.003/1k input + $0.015/1k output
    cost = compute_cost("claude-sonnet-4-6", 1000, 1000)
    assert cost == Decimal("0.018")


def test_compute_cost_unknown_model_returns_zero() -> None:
    """Unknown models report 0 cost (surface as 'unknown' in UI),
    never invent a price."""

    cost = compute_cost("mystery-model", 1000, 1000)
    assert cost == Decimal("0")


def test_record_writes_all_three_items(table: Any) -> None:
    store = TokenUsageStore(table)
    now = int(time.time())
    store.record(_usage(ts=now))

    date = _date_key(now)
    org_row = store.daily_org_total("org-a", date)
    agent_row = store.daily_agent_total("org-a", "bot-1", date)
    assert int(org_row["input_tokens"]) == 100
    assert int(agent_row["output_tokens"]) == 50

    # Raw event item exists with TTL.
    resp = table.scan()
    event_rows = [i for i in resp["Items"] if i["pk"].startswith("TOKENEVENT#")]
    assert len(event_rows) == 1
    assert "ttl" in event_rows[0]


def test_aggregates_sum_across_multiple_records(table: Any) -> None:
    """Multiple records on the same day stack atomically via ADD."""

    store = TokenUsageStore(table)
    now = int(time.time())
    for _ in range(5):
        store.record(_usage(ts=now))

    row = store.daily_org_total("org-a", _date_key(now))
    assert int(row["input_tokens"]) == 500
    assert int(row["output_tokens"]) == 250
    assert int(row["invocations"]) == 5


def test_separate_days_separate_rows(table: Any) -> None:
    store = TokenUsageStore(table)
    # Day 1
    day1 = 1700000000  # 2023-11-14 UTC
    store.record(_usage(ts=day1))
    # Day 2
    day2 = 1700086400  # +24h
    store.record(_usage(ts=day2))

    assert store.daily_org_total("org-a", _date_key(day1))["input_tokens"] == 100
    assert store.daily_org_total("org-a", _date_key(day2))["input_tokens"] == 100


def test_separate_agents_separate_agent_rows(table: Any) -> None:
    """Per-agent aggregates isolate one strategy's cost from another's."""

    store = TokenUsageStore(table)
    now = int(time.time())
    store.record(_usage(agent="bot-1", ts=now, input_t=100))
    store.record(_usage(agent="bot-2", ts=now, input_t=200))

    a1 = store.daily_agent_total("org-a", "bot-1", _date_key(now))
    a2 = store.daily_agent_total("org-a", "bot-2", _date_key(now))
    assert int(a1["input_tokens"]) == 100
    assert int(a2["input_tokens"]) == 200
    # Org total sums them
    org = store.daily_org_total("org-a", _date_key(now))
    assert int(org["input_tokens"]) == 300
    assert int(org["invocations"]) == 2


def test_orgs_isolated(table: Any) -> None:
    """Token usage of org A must never appear in org B's total."""

    store = TokenUsageStore(table)
    now = int(time.time())
    store.record(_usage(org="org-a", ts=now, input_t=100))
    store.record(_usage(org="org-b", ts=now, input_t=500))

    a = store.daily_org_total("org-a", _date_key(now))
    b = store.daily_org_total("org-b", _date_key(now))
    assert int(a["input_tokens"]) == 100
    assert int(b["input_tokens"]) == 500
