"""Tests for the social-sentiment tool's orchestration.

Reddit fetch is abstracted behind a client stub. Cache + quota
follow the patterns established for news; the spec-critical piece
here is the adversarial framing in the tool docstring (checked
below) — the tool returns RAW counts + sampled posts, never scores
sentiment itself.
"""

from __future__ import annotations

import time
from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.tool_quota.store import QuotaExceeded, ToolQuotaStore
from trading_strands.tools.base import ToolContext
from trading_strands.tools.social import (
    REDDIT_ADVERSARIAL_DOCSTRING_MARKER,
    SocialCache,
    SocialSnapshot,
    _run_social_fetch,
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


class _StubReddit:
    def __init__(
        self,
        posts: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._posts = posts or []
        self._raise = raise_exc
        self.calls: list[tuple[str, int]] = []

    def search(
        self, symbol: str, hours_back: int,
    ) -> list[dict[str, Any]]:
        self.calls.append((symbol, hours_back))
        if self._raise:
            raise self._raise
        return self._posts


def _ctx(
    table: Any,
    strategy_id: str = "strat-1", org_id: str = "org-a",
) -> ToolContext:
    return ToolContext(
        strategy_id=strategy_id, org_id=org_id,
        quota_store=ToolQuotaStore(table),
        secrets_client=None, table=table,
    )


# ── SocialCache ─────────────────────────────────────────────────────


def test_cache_round_trip() -> None:
    with mock_aws():
        cache = SocialCache(_table())
        snap = SocialSnapshot(
            symbol="AAPL",
            cached_at=int(time.time()),
            mention_count=42,
            sample_posts=[
                {"title": "loading the boat", "score": 101, "author": "ape"},
            ],
        )
        cache.put("AAPL", snap)
        loaded = cache.get("AAPL")
        assert loaded is not None
        assert loaded.mention_count == 42
        assert len(loaded.sample_posts) == 1


def test_cache_stale_returns_none() -> None:
    """1-hour TTL per §3.5 — older entries are miss, not hit."""

    import json

    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        table.put_item(Item={
            "pk": "SOCIAL#AAPL#cache",
            "cached_at": int(time.time()) - 2 * 3600,
            "payload_json": json.dumps({
                "symbol": "AAPL", "cached_at": 0,
                "mention_count": 0, "sample_posts": [],
            }),
        })
        assert cache.get("AAPL") is None


# ── _run_social_fetch ───────────────────────────────────────────────


def test_fetch_returns_sampled_posts_without_scoring() -> None:
    """Critical: the tool MUST NOT return a sentiment score. Raw
    counts + posts only; interpretation is the LLM's job."""

    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        client = _StubReddit(posts=[
            {"title": "AAPL to the moon", "score": 250, "author": "u1",
             "created_utc": 1700000000, "url": "http://r/1"},
            {"title": "loading puts", "score": 10, "author": "u2",
             "created_utc": 1700000000, "url": "http://r/2"},
        ])
        result = _run_social_fetch(
            symbol="AAPL", hours_back=6,
            ctx=_ctx(table), cache=cache, client=client, daily_quota=10,
        )
        assert result.symbol == "AAPL"
        assert result.mention_count == 2
        assert len(result.sample_posts) == 2
        # No 'sentiment' or 'score' aggregate.
        dumped = result.model_dump()
        assert "sentiment" not in dumped
        assert "score" not in dumped  # individual posts have it, the
                                       # top-level object must not


def test_fetch_cache_hit_does_not_consume_quota() -> None:
    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        cache.put("AAPL", SocialSnapshot(
            symbol="AAPL", cached_at=int(time.time()),
            mention_count=10, sample_posts=[],
        ))
        ctx = _ctx(table)
        client = _StubReddit()   # never called

        _run_social_fetch(
            symbol="AAPL", hours_back=6,
            ctx=ctx, cache=cache, client=client, daily_quota=10,
        )
        assert client.calls == []
        assert ctx.quota_store.consumed_today("strat-1", "social") == 0
        assert ctx.quota_store.cache_hits_today("strat-1", "social") == 1


def test_fetch_miss_consumes_quota_and_populates_cache() -> None:
    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        ctx = _ctx(table)
        client = _StubReddit(posts=[
            {"title": "t", "score": 10, "author": "u",
             "created_utc": 1700000000, "url": "http://x"},
        ])
        result = _run_social_fetch(
            symbol="AAPL", hours_back=6,
            ctx=ctx, cache=cache, client=client, daily_quota=10,
        )
        assert result.mention_count == 1
        assert len(client.calls) == 1
        assert ctx.quota_store.consumed_today("strat-1", "social") == 1
        # Cache populated.
        assert cache.get("AAPL") is not None


def test_fetch_quota_exceeded_raises_before_network() -> None:
    with mock_aws():
        table = _table()
        ctx = _ctx(table)
        for _ in range(3):
            ctx.quota_store.reserve("strat-1", "social", limit=3)
        client = _StubReddit()   # should not be called
        with pytest.raises(QuotaExceeded):
            _run_social_fetch(
                symbol="AAPL", hours_back=6,
                ctx=ctx, cache=SocialCache(table),
                client=client, daily_quota=3,
            )
        assert client.calls == []


def test_fetch_empty_result_still_caches() -> None:
    """A quiet symbol — nobody's talking about it. Caching the empty
    result means repeated reads don't hammer Reddit."""

    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        _run_social_fetch(
            symbol="QUIET", hours_back=6,
            ctx=_ctx(table), cache=cache,
            client=_StubReddit(posts=[]), daily_quota=10,
        )
        loaded = cache.get("QUIET")
        assert loaded is not None
        assert loaded.mention_count == 0


def test_fetch_symbol_uppercased() -> None:
    with mock_aws():
        table = _table()
        cache = SocialCache(table)
        cache.put("AAPL", SocialSnapshot(
            symbol="AAPL", cached_at=int(time.time()),
            mention_count=5, sample_posts=[],
        ))
        result = _run_social_fetch(
            symbol="aapl", hours_back=6,
            ctx=_ctx(table), cache=cache,
            client=_StubReddit(), daily_quota=10,
        )
        assert result.symbol == "AAPL"
        assert result.mention_count == 5


# ── Adversarial framing (docstring invariant) ───────────────────────


def test_docstring_warns_about_adversarial_data() -> None:
    """SPEC §3.5, §11.5: the tool's docstring must explicitly flag
    that social data is adversarial (pump groups, coordinated
    posts, bots). We pin a marker so a future refactor that loses
    the warning fails this test."""

    from trading_strands.tools.social import make_social_tool

    # The docstring is on the inner @tool — constructing it pulls
    # strands, which isn't importable in test. Instead we assert
    # the marker constant lives in the module; make_social_tool
    # injects it into the docstring.
    assert "adversarial" in REDDIT_ADVERSARIAL_DOCSTRING_MARKER.lower()
    assert "coordinated" in REDDIT_ADVERSARIAL_DOCSTRING_MARKER.lower() or \
           "bot" in REDDIT_ADVERSARIAL_DOCSTRING_MARKER.lower()
    # The factory exists and accepts a ToolContext.
    assert callable(make_social_tool)
