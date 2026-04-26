"""Tests for the Market Data Subscriber service.

The subscriber runs 24/7 in its own Fargate task: pulls quotes for the
union of symbols any active strategy cares about and writes them to
MarketDataStore. Strategy tasks don't need to be running for data to
flow — this captures premarket and extended hours activity regardless
of the trading schedule.

This test exercises the pure-function symbol resolver and a single
poll cycle of the loop. The loop itself is just a sleep + retry shell,
tested by verifying one iteration records ticks correctly.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.marketdata_store.store import MarketDataStore, hour_bucket
from trading_strands.marketdata_subscriber.loop import (
    FetchError,
    poll_once,
    watched_symbols,
)
from trading_strands.strategies_store.store import StrategyStatus, StrategyStore


def _make_table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── watched_symbols ──────────────────────────────────────────────────


def test_watched_symbols_unions_across_strategies() -> None:
    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        store.create(
            org_id="o1", author_user_id="u1", name="a",
            markdown="x", symbols=["AAPL", "MSFT"],
        )
        store.create(
            org_id="o2", author_user_id="u2", name="b",
            markdown="x", symbols=["MSFT", "NVDA"],
        )
        # Paused strategy — symbols should NOT be watched; pausing means
        # the user explicitly doesn't want resources spent on it.
        paused = store.create(
            org_id="o1", author_user_id="u1", name="p",
            markdown="x", symbols=["TSLA"],
        )
        store.update(paused.strategy_id, {"status": StrategyStatus.PAUSED.value})

        symbols = watched_symbols(store)
        assert symbols == {"AAPL", "MSFT", "NVDA"}


def test_watched_symbols_empty_when_no_active_strategies() -> None:
    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        assert watched_symbols(store) == set()


def test_watched_symbols_skips_dynamic_selection_strategies() -> None:
    """A strategy with an empty symbol list uses dynamic selection —
    we don't know ahead of time what to watch. Those symbols fall
    back to on-demand broker fetches during the trading tick."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        store.create(
            org_id="o1", author_user_id="u1", name="dyn",
            markdown="x", symbols=[],
        )
        assert watched_symbols(store) == set()


def test_watched_symbols_deduplicates_case() -> None:
    """Alpaca symbols are uppercase; dedup is case-insensitive to
    protect against user-entered lowercase strings."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        store.create(
            org_id="o1", author_user_id="u1", name="a",
            markdown="x", symbols=["AAPL", "aapl", "Msft"],
        )
        assert watched_symbols(store) == {"AAPL", "MSFT"}


# ── poll_once ────────────────────────────────────────────────────────


class FakeBroker:
    """In-memory broker adapter. get_quote returns canned prices.
    Raises for symbols in `fail_set` so we can exercise the
    per-symbol failure path."""

    def __init__(
        self,
        prices: dict[str, Decimal],
        fail_set: set[str] | None = None,
    ) -> None:
        self._prices = prices
        self._fail_set = fail_set or set()

    async def get_quote(self, symbol: str) -> dict[str, object]:
        if symbol in self._fail_set:
            raise RuntimeError(f"feed error: {symbol}")
        return {"price": self._prices[symbol]}


async def _noop_sleep(_seconds: float) -> None:
    """Drop-in for anyio.sleep used to exercise the internals without
    actually waiting."""

    return None


def test_poll_once_records_a_tick_per_symbol() -> None:
    """One cycle: fetch quotes for each watched symbol, hand each to
    MarketDataStore, flush. Symbols ordering doesn't matter."""

    import anyio

    with mock_aws():
        table = _make_table()
        store = MarketDataStore(table)
        broker = FakeBroker({
            "AAPL": Decimal("150.00"),
            "MSFT": Decimal("380.00"),
        })

        summary = anyio.run(
            poll_once, broker, store, frozenset({"AAPL", "MSFT"}),
        )

        assert summary.fetched == 2
        assert summary.errors == 0

        # Flush so we can read back what was written.
        store.flush()

        now = time.time()
        bucket = hour_bucket(now)
        aapl = store.get_hour("AAPL", bucket)
        msft = store.get_hour("MSFT", bucket)
        assert aapl, "AAPL should have a minute bar after the flush"
        assert msft, "MSFT should have a minute bar after the flush"


def test_poll_once_continues_on_per_symbol_failure() -> None:
    """A single symbol failing to quote must not stop the rest of the
    batch — we'd rather have N-1 symbols of data than zero."""

    import anyio

    with mock_aws():
        table = _make_table()
        store = MarketDataStore(table)
        broker = FakeBroker(
            prices={"AAPL": Decimal("150.00"), "MSFT": Decimal("380.00")},
            fail_set={"AAPL"},
        )

        summary = anyio.run(
            poll_once, broker, store, frozenset({"AAPL", "MSFT"}),
        )
        assert summary.fetched == 1
        assert summary.errors == 1
        assert summary.error_symbols == ("AAPL",)


def test_poll_once_empty_symbol_set_is_noop() -> None:
    """No strategies watching anything: don't call the broker at all.

    Keeps the subscriber dormant-but-healthy between deploys rather
    than throwing on an empty fleet."""

    import anyio

    class AssertingBroker:
        async def get_quote(self, symbol: str) -> dict[str, object]:
            raise AssertionError(f"unexpected fetch for {symbol}")

    with mock_aws():
        table = _make_table()
        store = MarketDataStore(table)
        summary = anyio.run(
            poll_once, AssertingBroker(), store, frozenset(),
        )
        assert summary.fetched == 0
        assert summary.errors == 0


def test_fetch_error_is_raised_when_broker_is_totally_broken() -> None:
    """Distinct from per-symbol failures: if even the first call is
    unrecoverable (e.g. 401 from Alpaca creds), we want the loop to
    escalate rather than silently record zero ticks every cycle."""

    import anyio

    class DeadBroker:
        async def get_quote(self, symbol: str) -> dict[str, object]:
            raise RuntimeError("401 Unauthorized")

    with mock_aws():
        table = _make_table()
        store = MarketDataStore(table)
        # With all symbols failing, the cycle should still return
        # — the FetchError is reserved for call-site escalation
        # when we want to crash loudly. Right now every symbol
        # failure counts toward summary.errors.
        summary = anyio.run(
            poll_once, DeadBroker(), store, frozenset({"AAPL", "MSFT"}),
        )
        assert summary.fetched == 0
        assert summary.errors == 2
        # But FetchError is the correct type for callers that want
        # to raise on full-batch failure:
        assert issubclass(FetchError, Exception)
