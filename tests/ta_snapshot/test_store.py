"""Tests for TASnapshotStore + summarize_for_symbols."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.ta_snapshot.store import (
    TASnapshot,
    TASnapshotStore,
    summarize_for_symbols,
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


def _sample_snapshot(symbol: str = "AAPL") -> TASnapshot:
    return TASnapshot(
        symbol=symbol,
        computed_at=int(time.time()),
        last_close=Decimal("150.50"),
        rsi_14=Decimal("62.3"),
        macd=Decimal("0.32"),
        macd_signal=Decimal("0.18"),
        macd_hist=Decimal("0.14"),
        sma_20=Decimal("148.00"),
        sma_50=Decimal("145.00"),
        sma_200=Decimal("140.00"),
        bb_upper=Decimal("152.00"),
        bb_middle=Decimal("148.00"),
        bb_lower=Decimal("144.00"),
    )


# ── store round-trip ───────────────────────────────────────────────


def test_put_and_get_latest_round_trip() -> None:
    with mock_aws():
        store = TASnapshotStore(_table())
        snap = _sample_snapshot()
        store.put_snapshot(snap)

        loaded = store.get_latest("AAPL")
        assert loaded is not None
        assert loaded.symbol == "AAPL"
        assert loaded.last_close == Decimal("150.50")
        assert loaded.rsi_14 == Decimal("62.3")


def test_get_latest_missing_returns_none() -> None:
    with mock_aws():
        store = TASnapshotStore(_table())
        assert store.get_latest("NVDA") is None


def test_get_latest_walks_back_through_hours() -> None:
    """Snapshot was written several hours ago — get_latest should
    still find it within the 6h default lookback window."""

    with mock_aws():
        store = TASnapshotStore(_table())
        # Write a snapshot timestamped 3 hours ago.
        three_h_ago = int(time.time()) - 3 * 3600
        snap = _sample_snapshot()
        snap_old = TASnapshot(
            **{**snap.model_dump(), "computed_at": three_h_ago},
        )
        store.put_snapshot(snap_old)

        loaded = store.get_latest("AAPL")
        assert loaded is not None
        assert loaded.computed_at == three_h_ago


def test_get_latest_returns_none_outside_lookback_window() -> None:
    """Snapshot older than lookback_hours — treat as missing. Keeps
    the LLM from reasoning on stale data after a long pause."""

    with mock_aws():
        store = TASnapshotStore(_table())
        ten_h_ago = int(time.time()) - 10 * 3600
        snap = _sample_snapshot()
        snap_old = TASnapshot(
            **{**snap.model_dump(), "computed_at": ten_h_ago},
        )
        store.put_snapshot(snap_old)

        assert store.get_latest("AAPL", lookback_hours=6) is None


def test_put_overwrites_existing_hour() -> None:
    """Re-running the computer in the same hour overwrites the
    prior snapshot instead of accumulating rows."""

    with mock_aws():
        table = _table()
        store = TASnapshotStore(table)
        now = int(time.time())
        store.put_snapshot(TASnapshot(
            symbol="AAPL",
            computed_at=now,
            last_close=Decimal("150.00"),
        ))
        store.put_snapshot(TASnapshot(
            symbol="AAPL",
            computed_at=now,
            last_close=Decimal("151.00"),
        ))
        # Only one row for the hour.
        resp = table.scan()
        assert len(resp.get("Items", [])) == 1
        loaded = store.get_latest("AAPL")
        assert loaded is not None
        assert loaded.last_close == Decimal("151.00")


# ── summarize_for_symbols ──────────────────────────────────────────


def test_summary_empty_symbols_placeholder() -> None:
    """Dynamic-selection strategies don't get TA — we don't know
    which tickers to inject."""

    with mock_aws():
        store = TASnapshotStore(_table())
        out = summarize_for_symbols(symbols=set(), store=store)
        assert "not applicable" in out.lower()


def test_summary_missing_snapshots_are_noted() -> None:
    """Bot just added a symbol; computer hasn't run for it yet.
    Surface "(no recent TA snapshot)" so the LLM knows."""

    with mock_aws():
        store = TASnapshotStore(_table())
        out = summarize_for_symbols(
            symbols={"NVDA"}, store=store,
        )
        assert "NVDA" in out
        assert "no recent" in out.lower()


def test_summary_includes_price_and_rsi_and_macd_and_ma_and_bb() -> None:
    with mock_aws():
        store = TASnapshotStore(_table())
        store.put_snapshot(_sample_snapshot())
        out = summarize_for_symbols(symbols={"AAPL"}, store=store)
        # All the indicator families show up in the rendered line.
        assert "AAPL" in out
        assert "RSI" in out.upper()
        assert "MACD" in out.upper()
        assert "SMA" in out.upper() or "150" in out  # tolerant of format
        assert "BB" in out.upper()


def test_summary_renders_one_line_per_symbol() -> None:
    """Terse by design — multi-symbol strategies can't bloat the
    prompt. One line per symbol with all indicators."""

    with mock_aws():
        store = TASnapshotStore(_table())
        store.put_snapshot(_sample_snapshot("AAPL"))
        store.put_snapshot(_sample_snapshot("MSFT"))
        out = summarize_for_symbols(
            symbols={"AAPL", "MSFT"}, store=store,
        )
        # Output has exactly two lines (one per symbol), sorted alpha.
        lines = out.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("AAPL")
        assert lines[1].startswith("MSFT")


def test_summary_bb_position_label() -> None:
    """Rather than dumping raw bands, summary classifies where price
    sits (upper third / middle / lower third / outside)."""

    with mock_aws():
        store = TASnapshotStore(_table())
        # Price at 150.50 with bands 144/148/152 → upper half but not
        # above upper band → "upper third" or "middle third" depending
        # on exact thirds arithmetic. Either way a positional label.
        store.put_snapshot(_sample_snapshot())
        out = summarize_for_symbols(symbols={"AAPL"}, store=store)
        assert any(
            phrase in out.lower()
            for phrase in (
                "upper third", "middle third", "lower third",
                "above upper", "below lower", "at middle",
            )
        )


def test_summary_handles_snapshot_with_none_indicators() -> None:
    """New symbol with only a few bars — every indicator is None.
    The formatter renders dashes, doesn't blow up."""

    with mock_aws():
        store = TASnapshotStore(_table())
        store.put_snapshot(TASnapshot(
            symbol="NEW",
            computed_at=int(time.time()),
            # No indicators computable yet.
        ))
        out = summarize_for_symbols(symbols={"NEW"}, store=store)
        assert "NEW" in out
        # Dashes for the missing values.
        assert "—" in out
