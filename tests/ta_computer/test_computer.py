"""Tests for the TA computer Lambda's pure-function core.

The computer walks active strategies' symbol sets, pulls recent bars
from MarketDataStore, computes a TASnapshot per symbol, writes it.

Tests cover: symbol collection (union, filter-active), close-series
extraction from minute-bar maps, write fanout.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.marketdata_store.store import MarketBar, MarketDataStore
from trading_strands.strategies_store.store import StrategyStatus, StrategyStore
from trading_strands.ta_computer.computer import (
    closes_from_hour_map,
    collect_watched_symbols,
    run_compute_for_symbol,
)
from trading_strands.ta_snapshot.store import TASnapshotStore


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── collect_watched_symbols ────────────────────────────────────────


def test_collect_union_across_active_strategies() -> None:
    with mock_aws():
        table = _table()
        store = StrategyStore(table)
        store.create(
            org_id="o1", author_user_id="u1", name="a",
            markdown="x", symbols=["AAPL", "MSFT"],
        )
        store.create(
            org_id="o2", author_user_id="u2", name="b",
            markdown="x", symbols=["MSFT", "NVDA"],
        )
        assert collect_watched_symbols(store) == {"AAPL", "MSFT", "NVDA"}


def test_collect_excludes_paused_and_stopped() -> None:
    with mock_aws():
        table = _table()
        store = StrategyStore(table)
        active = store.create(
            org_id="o1", author_user_id="u1", name="live",
            markdown="x", symbols=["AAPL"],
        )
        _ = active
        paused = store.create(
            org_id="o1", author_user_id="u1", name="paused",
            markdown="x", symbols=["MSFT"],
        )
        store.update(paused.strategy_id, {"status": StrategyStatus.PAUSED.value})
        stopped = store.create(
            org_id="o1", author_user_id="u1", name="stopped",
            markdown="x", symbols=["NVDA"],
        )
        store.update(stopped.strategy_id, {"status": StrategyStatus.STOPPED.value})

        assert collect_watched_symbols(store) == {"AAPL"}


def test_collect_uppercases_symbols() -> None:
    """Lowercase/mixed entries normalize to uppercase — same treatment
    as the market-data subscriber uses."""

    with mock_aws():
        table = _table()
        store = StrategyStore(table)
        store.create(
            org_id="o1", author_user_id="u1", name="a",
            markdown="x", symbols=["aapl", "Msft"],
        )
        assert collect_watched_symbols(store) == {"AAPL", "MSFT"}


def test_collect_empty_when_no_active() -> None:
    with mock_aws():
        store = StrategyStore(_table())
        assert collect_watched_symbols(store) == set()


# ── closes_from_hour_map ───────────────────────────────────────────


def test_closes_from_hour_map_preserves_order() -> None:
    """Minute-bar maps key on "MM" strings; closes must be returned
    in minute-ascending order so indicator math sees the right
    sequence."""

    hour_map = {
        "05": {"close": "102.00"},
        "02": {"close": "101.00"},
        "09": {"close": "103.00"},
        "00": {"close": "100.00"},
    }
    closes = closes_from_hour_map(hour_map)
    assert closes == [
        Decimal("100.00"),
        Decimal("101.00"),
        Decimal("102.00"),
        Decimal("103.00"),
    ]


def test_closes_from_hour_map_skips_malformed() -> None:
    """Bars missing 'close' (should never happen, but defensively):
    skip rather than crash the whole run."""

    hour_map = {
        "00": {"close": "100.00"},
        "01": {"open": "100.50"},  # no close
        "02": {"close": "102.00"},
    }
    closes = closes_from_hour_map(hour_map)
    assert closes == [Decimal("100.00"), Decimal("102.00")]


def test_closes_from_hour_map_empty() -> None:
    assert closes_from_hour_map({}) == []


# ── run_compute_for_symbol ──────────────────────────────────────────


def _seed_price_history(
    md_store: MarketDataStore, symbol: str, num_minutes: int,
) -> None:
    """Write num_minutes of synthetic price history for symbol, each
    minute offset from now. Alternating ±0.5 so EMAs and RSIs have
    data to churn on."""

    now = int(time.time())
    for i in range(num_minutes):
        ts = now - (num_minutes - i) * 60
        price = Decimal("100") + (Decimal("0.5") if i % 2 == 0 else Decimal("-0.5"))
        md_store.record_tick(MarketBar(
            timestamp=ts, symbol=symbol, price=price,
        ))
    md_store.flush()


def test_run_compute_writes_snapshot_with_indicators() -> None:
    """Happy path: enough bars → snapshot has non-None indicators."""

    with mock_aws():
        table = _table()
        md_store = MarketDataStore(table)
        snap_store = TASnapshotStore(table)
        _seed_price_history(md_store, "AAPL", num_minutes=300)

        run_compute_for_symbol(
            symbol="AAPL",
            md_store=md_store,
            snap_store=snap_store,
        )

        loaded = snap_store.get_latest("AAPL")
        assert loaded is not None
        assert loaded.symbol == "AAPL"
        # Enough data → all indicators populated.
        assert loaded.rsi_14 is not None
        assert loaded.sma_20 is not None
        assert loaded.bb_upper is not None


def test_run_compute_handles_insufficient_data_gracefully() -> None:
    """Only a handful of bars → most indicators are None but the
    snapshot still writes with last_close set. Downstream consumers
    render dashes for the unavailable indicators."""

    with mock_aws():
        table = _table()
        md_store = MarketDataStore(table)
        snap_store = TASnapshotStore(table)
        _seed_price_history(md_store, "NVDA", num_minutes=5)

        run_compute_for_symbol(
            symbol="NVDA",
            md_store=md_store,
            snap_store=snap_store,
        )

        loaded = snap_store.get_latest("NVDA")
        assert loaded is not None
        assert loaded.last_close is not None
        # Most indicators require more bars — should be None.
        assert loaded.sma_200 is None


def test_run_compute_skips_symbol_with_no_bars() -> None:
    """Symbol in the active set but the subscriber hasn't written any
    bars yet — don't write a useless empty snapshot."""

    with mock_aws():
        table = _table()
        md_store = MarketDataStore(table)
        snap_store = TASnapshotStore(table)

        run_compute_for_symbol(
            symbol="GHOST",
            md_store=md_store,
            snap_store=snap_store,
        )

        # No snapshot persisted.
        assert snap_store.get_latest("GHOST") is None
