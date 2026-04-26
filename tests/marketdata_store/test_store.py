"""Tests for the market data island store."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_strands.marketdata_store.store import (
    MarketBar,
    MarketDataStore,
    hour_bucket,
)


def test_hour_bucket_format() -> None:
    # 2026-04-26 14:35:12 UTC -> 2026042614
    # unix ts for that moment:
    import calendar
    ts = calendar.timegm((2026, 4, 26, 14, 35, 12, 0, 0, 0))
    assert hour_bucket(ts) == "2026042614"


def test_record_and_read_single_minute_after_rollover(table: Any) -> None:
    """Writes flush only when the minute rolls over for that symbol."""

    store = MarketDataStore(table)
    import calendar
    ts_1032_05 = calendar.timegm((2026, 4, 26, 10, 32, 5, 0, 0, 0))
    ts_1032_45 = calendar.timegm((2026, 4, 26, 10, 32, 45, 0, 0, 0))
    ts_1033_00 = calendar.timegm((2026, 4, 26, 10, 33, 0, 0, 0, 0))

    store.record_tick(MarketBar(ts_1032_05, "SPY", Decimal("500.00")))
    store.record_tick(MarketBar(ts_1032_45, "SPY", Decimal("500.50")))
    # Minute still in progress — nothing should be readable yet.
    assert store.get_hour("SPY", "2026042610") == {}

    # New minute arrives for the same symbol — prior minute flushes.
    store.record_tick(MarketBar(ts_1033_00, "SPY", Decimal("501.00")))
    bars = store.get_hour("SPY", "2026042610")
    assert "32" in bars
    bar = bars["32"]
    assert bar["open"] == "500.00"
    assert bar["close"] == "500.50"
    assert bar["high"] == "500.50"
    assert bar["low"] == "500.00"
    assert int(bar["samples"]) == 2


def test_flush_writes_in_progress_minute(table: Any) -> None:
    store = MarketDataStore(table)
    import calendar
    ts = calendar.timegm((2026, 4, 26, 10, 32, 5, 0, 0, 0))

    store.record_tick(MarketBar(ts, "SPY", Decimal("500.00")))
    # Without flush, the minute is still buffered.
    assert store.get_hour("SPY", "2026042610") == {}

    store.flush()
    bars = store.get_hour("SPY", "2026042610")
    assert "32" in bars


def test_multiple_symbols_isolated(table: Any) -> None:
    store = MarketDataStore(table)
    import calendar
    ts = calendar.timegm((2026, 4, 26, 10, 32, 5, 0, 0, 0))

    store.record_tick(MarketBar(ts, "SPY", Decimal("500")))
    store.record_tick(MarketBar(ts, "QQQ", Decimal("450")))
    store.flush()

    spy = store.get_hour("SPY", "2026042610")
    qqq = store.get_hour("QQQ", "2026042610")
    assert "32" in spy and "32" in qqq
    assert spy["32"]["open"] == "500"
    assert qqq["32"]["open"] == "450"


def test_get_range_across_hour_boundary(table: Any) -> None:
    store = MarketDataStore(table)
    import calendar
    t1 = calendar.timegm((2026, 4, 26, 10, 45, 0, 0, 0, 0))
    t2 = calendar.timegm((2026, 4, 26, 11, 5, 0, 0, 0, 0))

    store.record_tick(MarketBar(t1, "SPY", Decimal("500")))
    store.record_tick(MarketBar(t2, "SPY", Decimal("501")))
    store.flush()

    bars = store.get_range("SPY", t1 - 60, t2 + 60)
    assert len(bars) == 2
    assert bars[0][0] == t1
    assert bars[1][0] == t2


def test_ttl_applied_on_first_write(table: Any) -> None:
    """Each bucket item carries a TTL of (retention_days) from first write.
    DDB TTL handles deletion; this just verifies we're writing the attribute.
    """

    store = MarketDataStore(table, retention_days=30)
    import calendar
    ts = calendar.timegm((2026, 4, 26, 10, 32, 5, 0, 0, 0))
    store.record_tick(MarketBar(ts, "SPY", Decimal("500")))
    store.flush()

    resp = table.get_item(Key={"pk": "MARKETDATA#SPY#2026042610"})
    item = resp["Item"]
    assert "ttl" in item
    # TTL should be ~30 days from now (when the test wrote it).
    import time
    now = int(time.time())
    assert item["ttl"] > now + 29 * 86400
    assert item["ttl"] < now + 31 * 86400
