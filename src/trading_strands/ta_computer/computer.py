"""TA computer Lambda — computes TA snapshots for every symbol any
active strategy watches, writes them to TASnapshotStore.

No external APIs. Reads minute bars from MarketDataStore, calls
indicator math from trading_strands.ta_snapshot.indicators, persists
via TASnapshotStore.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

import structlog

from trading_strands.marketdata_store.store import (
    MarketDataStore,
    hour_bucket,
)
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)
from trading_strands.ta_snapshot.indicators import compute_snapshot
from trading_strands.ta_snapshot.store import TASnapshot, TASnapshotStore

logger = structlog.get_logger()

# How far back to gather minute bars when computing indicators.
# 200-minute SMA needs at least 200 bars; we fetch 4 hours (240 min)
# so a fresh market-open run has enough history after a weekend.
# Beyond that, extra bars don't help — indicators look at the most
# recent window.
_BAR_LOOKBACK_HOURS = 4


def collect_watched_symbols(store: StrategyStore) -> set[str]:
    """Return the union of symbols across every ACTIVE strategy.

    Uppercased to match Alpaca's canonical form and the subscriber's
    normalization (so "aapl" and "AAPL" in different strategies
    don't become two symbols here).
    """

    out: set[str] = set()
    for s in store.list_all():
        if s.status != StrategyStatus.ACTIVE:
            continue
        for sym in s.symbols:
            if sym:
                out.add(sym.upper())
    return out


def closes_from_hour_map(
    hour_map: dict[str, dict[str, Any]],
) -> list[Decimal]:
    """Turn a {minute_key: bar} map into an ordered close-price list.

    Bars missing 'close' are skipped (shouldn't happen given the
    subscriber's shape, but defensive). Sort by minute key so the
    indicator math sees strictly increasing time.
    """

    result: list[Decimal] = []
    for minute_key in sorted(hour_map.keys()):
        bar = hour_map[minute_key]
        close = bar.get("close")
        if close is None:
            continue
        try:
            result.append(Decimal(str(close)))
        except Exception:
            logger.warning(
                "ta_computer.malformed_close", minute=minute_key,
            )
    return result


def _gather_recent_closes(
    symbol: str, md_store: MarketDataStore,
) -> list[Decimal]:
    """Pull minute bars from the last N hours and concatenate their
    closes in chronological order."""

    now = time.time()
    closes: list[Decimal] = []
    # Oldest hour first so closes end up chronologically ordered.
    for offset in range(_BAR_LOOKBACK_HOURS, -1, -1):
        ts = now - offset * 3600
        bucket = hour_bucket(ts)
        closes.extend(closes_from_hour_map(md_store.get_hour(symbol, bucket)))
    return closes


def run_compute_for_symbol(
    symbol: str,
    md_store: MarketDataStore,
    snap_store: TASnapshotStore,
) -> bool:
    """Compute + write a TA snapshot for one symbol.

    Returns True if a snapshot was written, False if nothing to
    compute (no bars for the symbol yet).
    """

    closes = _gather_recent_closes(symbol, md_store)
    if not closes:
        logger.info("ta_computer.no_bars", symbol=symbol)
        return False

    indicators = compute_snapshot(closes)
    snap = TASnapshot(
        symbol=symbol,
        computed_at=int(time.time()),
        last_close=indicators.get("last_close"),
        rsi_14=indicators.get("rsi_14"),
        macd=indicators.get("macd"),
        macd_signal=indicators.get("macd_signal"),
        macd_hist=indicators.get("macd_hist"),
        sma_20=indicators.get("sma_20"),
        sma_50=indicators.get("sma_50"),
        sma_200=indicators.get("sma_200"),
        bb_upper=indicators.get("bb_upper"),
        bb_middle=indicators.get("bb_middle"),
        bb_lower=indicators.get("bb_lower"),
    )
    snap_store.put_snapshot(snap)
    return True


def _run(
    table: Any,
) -> dict[str, Any]:
    """Main body of the Lambda. Split out so tests can inject a
    pre-populated table without going through handler()/boto3."""

    strategy_store = StrategyStore(table)
    md_store = MarketDataStore(table)
    snap_store = TASnapshotStore(table)

    symbols = collect_watched_symbols(strategy_store)
    written = 0
    skipped = 0
    errors = 0
    for sym in sorted(symbols):
        try:
            if run_compute_for_symbol(sym, md_store, snap_store):
                written += 1
            else:
                skipped += 1
        except Exception:
            errors += 1
            logger.exception("ta_computer.symbol_failed", symbol=sym)

    logger.info(
        "ta_computer.complete",
        total=len(symbols), written=written,
        skipped=skipped, errors=errors,
    )
    return {
        "total_symbols": len(symbols),
        "written": written,
        "skipped": skipped,
        "errors": errors,
    }


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. EventBridge invokes on a 5-minute cron
    during market hours (schedule lives in CDK)."""

    import boto3

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(os.environ["DYNAMODB_TABLE"])
    return _run(table)
