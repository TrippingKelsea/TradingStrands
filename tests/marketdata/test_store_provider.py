"""Tests for StoreBackedMarketDataProvider.

This is the provider that enables the v1 architecture where strategies
don't hold their own broker connections — they read prices written by
the dedicated subscriber service. The swap is opt-in via env flag; the
provider itself accepts a fallback broker so a missing/stale bar
doesn't leave a strategy with no price.
"""

from __future__ import annotations

import time
from decimal import Decimal

import anyio
import boto3
from moto import mock_aws

from trading_strands.marketdata.store_provider import (
    StoreBackedMarketDataProvider,
)
from trading_strands.marketdata_store.store import MarketBar, MarketDataStore


def _table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


class FakeBroker:
    """Stand-in fallback broker. Records calls so tests can assert
    the fallback was (or wasn't) used."""

    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = prices
        self.calls: list[str] = []

    async def get_quote(self, symbol: str) -> dict[str, object]:
        self.calls.append(symbol)
        return {"price": self._prices[symbol]}


def _seed_recent_bar(
    store: MarketDataStore, symbol: str, price: Decimal,
    age_seconds: float = 0.0,
) -> None:
    """Write one minute-bar with last_ts shifted by age_seconds ago."""

    ts = int(time.time() - age_seconds)
    store.record_tick(MarketBar(timestamp=ts, symbol=symbol, price=price))
    store.flush()


# ── get_price happy path ────────────────────────────────────────────


def test_get_price_reads_from_store_when_fresh() -> None:
    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("155.50"))
        broker = FakeBroker({"AAPL": Decimal("999")})  # should NOT be called

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
        )
        price = anyio.run(provider.get_price, "AAPL")

        assert price == Decimal("155.50")
        assert broker.calls == []


def test_get_prices_batch_reads_all_from_store() -> None:
    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("150"))
        _seed_recent_bar(store, "MSFT", Decimal("380"))
        broker = FakeBroker({})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
        )
        prices = anyio.run(provider.get_prices, {"AAPL", "MSFT"})

        assert prices == {"AAPL": Decimal("150"), "MSFT": Decimal("380")}
        assert broker.calls == []


# ── fallback ───────────────────────────────────────────────────────


def test_falls_back_to_broker_when_symbol_absent_from_store() -> None:
    """First tick after adding a new symbol — subscriber hasn't written
    anything for it yet. Falling back keeps the strategy alive."""

    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("150"))
        broker = FakeBroker({"NVDA": Decimal("800")})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
        )
        price = anyio.run(provider.get_price, "NVDA")

        assert price == Decimal("800")
        assert broker.calls == ["NVDA"]


def test_falls_back_when_bar_is_stale() -> None:
    """Subscriber may have died or is lagging — prefer fresh broker
    price over a stale stored one."""

    with mock_aws():
        store = MarketDataStore(_table())
        # 10 minutes old — well past the default 120s threshold.
        _seed_recent_bar(store, "AAPL", Decimal("150"), age_seconds=600)
        broker = FakeBroker({"AAPL": Decimal("152")})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
            staleness_threshold_seconds=120,
        )
        price = anyio.run(provider.get_price, "AAPL")

        assert price == Decimal("152")
        assert broker.calls == ["AAPL"]


def test_staleness_threshold_is_configurable() -> None:
    """A very tight threshold forces fallback even for just-written bars."""

    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("150"), age_seconds=3)
        broker = FakeBroker({"AAPL": Decimal("152")})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
            staleness_threshold_seconds=1,  # tight
        )
        price = anyio.run(provider.get_price, "AAPL")
        assert price == Decimal("152")
        assert broker.calls == ["AAPL"]


def test_get_prices_uses_fallback_per_missing_symbol() -> None:
    """Mixed batch: AAPL is in the store, NVDA isn't. Partial fallback."""

    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("150"))
        broker = FakeBroker({"NVDA": Decimal("800")})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
        )
        prices = anyio.run(provider.get_prices, {"AAPL", "NVDA"})

        assert prices == {"AAPL": Decimal("150"), "NVDA": Decimal("800")}
        # Only NVDA went to the broker.
        assert broker.calls == ["NVDA"]


# ── error paths ─────────────────────────────────────────────────────


def test_raises_when_both_store_and_broker_fail() -> None:
    """If neither source can produce a price, the strategy needs to
    know — silently returning zero would cause bad trade sizing."""

    with mock_aws():
        store = MarketDataStore(_table())

        class DeadBroker:
            async def get_quote(self, symbol: str) -> dict[str, object]:
                raise RuntimeError("401")

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=DeadBroker(),
        )
        try:
            anyio.run(provider.get_price, "GONE")
        except RuntimeError as exc:
            assert "GONE" in str(exc) or "401" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")


# ── get_quote passthrough ──────────────────────────────────────────


def test_get_quote_always_goes_to_broker() -> None:
    """Full quotes (bid/ask/sizes) aren't captured in MarketDataStore's
    minute-bar schema. Quote requests always go to the broker — the
    store-backed provider only optimizes price lookups."""

    with mock_aws():
        store = MarketDataStore(_table())
        _seed_recent_bar(store, "AAPL", Decimal("150"))
        broker = FakeBroker({"AAPL": Decimal("150")})

        provider = StoreBackedMarketDataProvider(
            store=store, fallback_broker=broker,
        )
        anyio.run(provider.get_quote, "AAPL")
        assert broker.calls == ["AAPL"]
