"""Tests for ToolQuotaStore.

Per-strategy per-day counter. Hard-stop semantics: once consumed ==
limit, subsequent reserves raise QuotaExceeded. Cache hits must not
count (§7.3 of tools.md).
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.tool_quota.store import (
    QuotaExceeded,
    ToolQuotaStore,
)


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── reserve + consumed ──────────────────────────────────────────────


def test_reserve_increments_counter() -> None:
    with mock_aws():
        store = ToolQuotaStore(_table())
        store.reserve("strat-1", "news", limit=10)
        store.reserve("strat-1", "news", limit=10)
        assert store.consumed_today("strat-1", "news") == 2


def test_reserve_respects_limit() -> None:
    """Once consumed reaches limit, reserve raises."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        for _ in range(3):
            store.reserve("strat-1", "news", limit=3)
        with pytest.raises(QuotaExceeded):
            store.reserve("strat-1", "news", limit=3)


def test_reserve_with_zero_limit_is_always_denied() -> None:
    """daily_quota=0 in the strategy config means disabled — reserve
    must reject immediately, before the tool code runs. Distinct from
    'unlimited' which we explicitly don't support (quota is a
    cost-control mechanism, unlimited would defeat it)."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        with pytest.raises(QuotaExceeded):
            store.reserve("strat-1", "news", limit=0)


def test_reserve_isolates_per_strategy() -> None:
    """strat-1 exhausting their news quota doesn't affect strat-2."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        for _ in range(5):
            store.reserve("strat-1", "news", limit=5)
        # strat-2's counter is untouched.
        store.reserve("strat-2", "news", limit=5)
        assert store.consumed_today("strat-2", "news") == 1


def test_reserve_isolates_per_tool() -> None:
    """Exhausting news quota doesn't prevent filings calls."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        for _ in range(3):
            store.reserve("strat-1", "news", limit=3)
        # filings is a separate counter.
        store.reserve("strat-1", "filings", limit=5)
        assert store.consumed_today("strat-1", "filings") == 1


def test_consumed_today_zero_before_any_reserves() -> None:
    with mock_aws():
        store = ToolQuotaStore(_table())
        assert store.consumed_today("strat-1", "news") == 0


# ── record_cache_hit ────────────────────────────────────────────────


def test_cache_hit_does_not_consume_quota() -> None:
    """§7.3: cache hits are essentially free — they must not count
    against the strategy's daily budget. Separate counter tracks
    them for cache-effectiveness observability."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        store.record_cache_hit("strat-1", "news")
        store.record_cache_hit("strat-1", "news")
        # External-call counter untouched.
        assert store.consumed_today("strat-1", "news") == 0
        # Cache-hit counter is tracked separately.
        assert store.cache_hits_today("strat-1", "news") == 2


# ── day boundary ────────────────────────────────────────────────────


def test_counter_rolls_over_at_utc_midnight() -> None:
    """Day key uses UTC-midnight rollover. A call today and a call
    tomorrow (simulated via injected date) go into distinct rows,
    so tomorrow starts fresh."""

    with mock_aws():
        store = ToolQuotaStore(_table())
        store.reserve("strat-1", "news", limit=3, _today="2026-04-27")
        store.reserve("strat-1", "news", limit=3, _today="2026-04-27")
        store.reserve("strat-1", "news", limit=3, _today="2026-04-27")

        # Tomorrow — quota resets.
        store.reserve("strat-1", "news", limit=3, _today="2026-04-28")
        assert store.consumed_today(
            "strat-1", "news", _today="2026-04-28",
        ) == 1
        assert store.consumed_today(
            "strat-1", "news", _today="2026-04-27",
        ) == 3


def test_counter_ttl_is_forward_dated() -> None:
    """Counters self-expire a bit after the day they cover — operators
    can inspect yesterday's usage today but year-old rows don't stick
    around. 30 days out gives enough window for post-hoc analysis."""

    with mock_aws():
        table = _table()
        store = ToolQuotaStore(table)
        store.reserve("strat-1", "news", limit=5)

        import time
        # Read the row back to confirm ttl landed.
        resp = table.scan()
        items = resp.get("Items", [])
        assert len(items) == 1
        assert int(items[0]["ttl"]) > int(time.time())
