"""Tests for the news tool.

Exercises the cache-first pattern (cache hit vs miss), quota
accounting (external call consumes, cache hit does not), and the
per-org credential lookup. HTTP is stubbed — never makes real calls.
"""

from __future__ import annotations

import time
from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.tool_quota.store import QuotaExceeded, ToolQuotaStore
from trading_strands.tools.base import ToolContext
from trading_strands.tools.news import (
    NewsCache,
    NewsItem,
    _run_news_fetch,
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


class _StubNewsClient:
    """In-process news client. Records calls, returns canned items."""

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._raise = raise_exc
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def fetch(
        self,
        symbols: tuple[str, ...],
        start_ts: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((symbols, start_ts))
        if self._raise:
            raise self._raise
        return self._items


def _ctx(table: Any, strategy_id: str = "strat-1", org_id: str = "org-a") -> ToolContext:
    return ToolContext(
        strategy_id=strategy_id, org_id=org_id,
        quota_store=ToolQuotaStore(table),
        secrets_client=None, table=table,
    )


# ── NewsCache round-trip ────────────────────────────────────────────


def test_cache_put_and_get() -> None:
    with mock_aws():
        cache = NewsCache(_table())
        items = [
            NewsItem(
                id=str(i), headline=f"h{i}", summary="s",
                url=f"http://x/{i}", source="alpaca",
                created_at=int(time.time()), symbols=["AAPL"],
            )
            for i in range(3)
        ]
        cache.put("AAPL", items)
        loaded = cache.get("AAPL")
        assert loaded is not None
        assert [it.id for it in loaded] == ["0", "1", "2"]


def test_cache_miss_returns_none() -> None:
    with mock_aws():
        cache = NewsCache(_table())
        assert cache.get("NVDA") is None


def test_cache_ttl_respected() -> None:
    """A cache entry older than 1h is treated as expired — caller
    should fetch fresh. Tested by injecting an older timestamp
    into the stored row."""

    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        # Write a stale row directly — cached_at 2h ago.
        two_h_ago = int(time.time()) - 2 * 3600
        import json
        table.put_item(Item={
            "pk": "NEWS#AAPL#cache",
            "cached_at": two_h_ago,
            "payload_json": json.dumps([]),
        })
        # Treat stale (>1h) as miss.
        assert cache.get("AAPL") is None


# ── _run_news_fetch orchestration ───────────────────────────────────


def test_cache_hit_does_not_consume_quota() -> None:
    """Fresh entry in cache → serve from cache, increment the
    cache-hit counter, skip the external call."""

    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        cache.put("AAPL", [NewsItem(
            id="1", headline="h", summary="s", url="http://x/1",
            source="alpaca", created_at=int(time.time()),
            symbols=["AAPL"],
        )])
        client = _StubNewsClient()  # never called in cache-hit path
        ctx = _ctx(table)

        result = _run_news_fetch(
            symbol="AAPL", hours_back=24,
            ctx=ctx, cache=cache, client=client, daily_quota=10,
        )
        assert [it.id for it in result] == ["1"]
        # Client not called.
        assert client.calls == []
        # Quota consumed 0, cache_hits 1.
        assert ctx.quota_store.consumed_today("strat-1", "news") == 0
        assert ctx.quota_store.cache_hits_today("strat-1", "news") == 1


def test_cache_miss_fetches_and_persists() -> None:
    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        client = _StubNewsClient(items=[
            {
                "id": "abc123",
                "headline": "AAPL beats estimates",
                "summary": "A summary",
                "url": "https://news.example/abc123",
                "source": "Reuters",
                "created_at": "2026-04-27T14:00:00Z",
                "symbols": ["AAPL"],
            },
        ])
        ctx = _ctx(table)

        result = _run_news_fetch(
            symbol="AAPL", hours_back=24,
            ctx=ctx, cache=cache, client=client, daily_quota=10,
        )
        assert len(result) == 1
        assert result[0].headline == "AAPL beats estimates"
        # Client was called once.
        assert len(client.calls) == 1
        # Cache now has the entry — a follow-up would hit it.
        loaded = cache.get("AAPL")
        assert loaded is not None
        assert loaded[0].id == "abc123"
        # Quota consumed (external call), cache_hits unchanged.
        assert ctx.quota_store.consumed_today("strat-1", "news") == 1
        assert ctx.quota_store.cache_hits_today("strat-1", "news") == 0


def test_quota_exceeded_raises_before_network() -> None:
    """Quota is reserved BEFORE the network call. When exhausted, the
    tool raises without ever hitting the external API (§7.2)."""

    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        client = _StubNewsClient()
        ctx = _ctx(table)

        # Pre-consume the full quota of 3.
        ctx.quota_store.reserve("strat-1", "news", limit=3)
        ctx.quota_store.reserve("strat-1", "news", limit=3)
        ctx.quota_store.reserve("strat-1", "news", limit=3)

        with pytest.raises(QuotaExceeded):
            _run_news_fetch(
                symbol="AAPL", hours_back=24,
                ctx=ctx, cache=cache, client=client, daily_quota=3,
            )
        # Client never called — quota check fired before network.
        assert client.calls == []


def test_zero_quota_raises_immediately() -> None:
    """daily_quota=0 (disabled) → raise even for first call."""

    with mock_aws():
        table = _table()
        ctx = _ctx(table)
        with pytest.raises(QuotaExceeded):
            _run_news_fetch(
                symbol="AAPL", hours_back=24,
                ctx=ctx, cache=NewsCache(table),
                client=_StubNewsClient(), daily_quota=0,
            )


def test_client_error_still_costs_quota() -> None:
    """§7.2: a failing external call still counts against quota —
    the request reached the endpoint; whether it 200'd doesn't
    change the cost model."""

    with mock_aws():
        table = _table()
        ctx = _ctx(table)
        client = _StubNewsClient(raise_exc=RuntimeError("alpaca 500"))
        with pytest.raises(RuntimeError):
            _run_news_fetch(
                symbol="AAPL", hours_back=24,
                ctx=ctx, cache=NewsCache(table),
                client=client, daily_quota=10,
            )
        # Quota was reserved before the call fired.
        assert ctx.quota_store.consumed_today("strat-1", "news") == 1


def test_hours_back_passed_to_client() -> None:
    """The start_ts computed from hours_back must be within tolerance
    of now - hours_back*3600. Operator-specified lookback has to
    reach the client accurately."""

    with mock_aws():
        table = _table()
        client = _StubNewsClient()
        _run_news_fetch(
            symbol="AAPL", hours_back=48,
            ctx=_ctx(table), cache=NewsCache(table),
            client=client, daily_quota=10,
        )
        assert len(client.calls) == 1
        _syms, start_ts = client.calls[0]
        expected = int(time.time()) - 48 * 3600
        # ±5s tolerance to cover test runtime.
        assert abs(start_ts - expected) < 5


def test_empty_news_list_still_cached() -> None:
    """No news doesn't mean 'no cache' — a quiet symbol would
    otherwise hit the API every time. Cache the empty list."""

    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        client = _StubNewsClient(items=[])
        _run_news_fetch(
            symbol="QUIET", hours_back=24,
            ctx=_ctx(table), cache=cache,
            client=client, daily_quota=10,
        )
        loaded = cache.get("QUIET")
        assert loaded == []


def test_symbol_uppercased_before_cache_lookup() -> None:
    """User-typed symbols may be lowercase; we canonicalize so the
    same cache row serves 'aapl' and 'AAPL'."""

    with mock_aws():
        table = _table()
        cache = NewsCache(table)
        cache.put("AAPL", [NewsItem(
            id="x", headline="h", summary="s", url="",
            source="a", created_at=int(time.time()), symbols=["AAPL"],
        )])
        client = _StubNewsClient()
        result = _run_news_fetch(
            symbol="aapl", hours_back=24,
            ctx=_ctx(table), cache=cache,
            client=client, daily_quota=10,
        )
        assert result and result[0].id == "x"
        # Served from cache — no external call.
        assert client.calls == []
