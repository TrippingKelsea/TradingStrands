"""Subscriber poll loop.

Simple for now: every N seconds, walk the active strategies, union
their symbol sets, fetch one quote per symbol, record each as a tick.
No websocket — this is the same pull pattern the orchestrator already
uses, just in a dedicated process so it runs 24/7 and so connections
don't multiply with strategy count.

When we upgrade to the Alpaca websocket feed, the shape below stays
the same: the websocket event handler calls `store.record_tick` with
incoming bars. `poll_once` becomes a no-op and `run_forever` becomes
"connect + stream until disconnect".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anyio
import structlog

from trading_strands.marketdata_store.store import MarketBar, MarketDataStore
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()


class FetchError(Exception):
    """Raised when the loop wants to signal total-feed failure — unused
    internally (per-symbol failures are counted), but callers can catch
    this to distinguish "configure-my-creds" from "market is closed"."""


@dataclass
class PollSummary:
    fetched: int
    errors: int
    error_symbols: tuple[str, ...] = ()


def watched_symbols(store: StrategyStore) -> set[str]:
    """Union of symbols across every ACTIVE strategy.

    Paused/stopped strategies are skipped — users paused for a reason,
    wasting subscriber bandwidth on their symbols is wrong. Dynamic-
    selection strategies (empty symbol list) contribute nothing here;
    when they pick a symbol, the trading task fetches it directly on
    the tick that uses it.

    Case-normalized to uppercase so user-entered 'aapl' matches Alpaca's
    'AAPL' in the store.
    """

    symbols: set[str] = set()
    for s in store.list_all():
        if s.status != StrategyStatus.ACTIVE:
            continue
        for sym in s.symbols:
            if sym:
                symbols.add(sym.upper())
    return symbols


async def poll_once(
    broker: Any,
    store: MarketDataStore,
    symbols: frozenset[str],
) -> PollSummary:
    """One iteration: fetch every symbol once, record each as a tick.

    Per-symbol errors are counted, not raised — one bad symbol
    shouldn't starve every other symbol of data. The caller decides
    whether sustained all-failures (count == len(symbols) repeatedly)
    warrants escalation.
    """

    if not symbols:
        return PollSummary(fetched=0, errors=0)

    fetched = 0
    errors: list[str] = []

    for symbol in sorted(symbols):
        try:
            quote = await broker.get_quote(symbol)
        except Exception:
            logger.exception("subscriber.quote_failed symbol=%s", symbol)
            errors.append(symbol)
            continue
        price = quote.get("price")
        if price is None:
            errors.append(symbol)
            continue
        if not isinstance(price, Decimal):
            price = Decimal(str(price))
        store.record_tick(MarketBar(
            timestamp=int(time.time()),
            symbol=symbol,
            price=price,
        ))
        fetched += 1

    return PollSummary(
        fetched=fetched, errors=len(errors), error_symbols=tuple(errors),
    )


async def run_forever(
    *,
    broker: Any,
    store: MarketDataStore,
    strategy_store: StrategyStore,
    poll_interval: float = 5.0,
    symbol_refresh_interval: float = 60.0,
    heartbeat_store: Any | None = None,
    heartbeat_agent_id: str = "marketdata-subscriber",
) -> None:
    """Main subscriber loop.

    Split cadence: fetch quotes every `poll_interval` seconds; refresh
    the watched-symbols set every `symbol_refresh_interval` seconds.
    Refreshing symbols is a full DDB scan, which we don't want to do
    on every tick.

    Errors in the symbol refresh (e.g. transient DDB failures) log
    and continue with the prior symbol set — stale symbols are fine;
    crashing the service would be worse.

    heartbeat_store (optional): if set, beats on every cycle so the
    Platform Supervisor can flag a stuck subscriber.
    """

    symbols: frozenset[str] = frozenset()
    last_refresh = 0.0

    while True:
        # Beat at the top of the cycle so a slow feed still registers
        # the subscriber as alive to the supervisor.
        if heartbeat_store is not None:
            try:
                heartbeat_store.beat(
                    agent_type="subscriber",
                    agent_id=heartbeat_agent_id,
                )
            except Exception:
                await logger.aexception("subscriber.heartbeat_failed")

        now = time.monotonic()
        if now - last_refresh >= symbol_refresh_interval:
            try:
                symbols = frozenset(watched_symbols(strategy_store))
                await logger.ainfo(
                    "subscriber.symbols_refreshed count=%d", len(symbols),
                )
            except Exception:
                await logger.aexception("subscriber.symbols_refresh_failed")
            last_refresh = now

        try:
            summary = await poll_once(broker, store, symbols)
            if summary.errors and summary.errors == len(symbols) and symbols:
                # Every symbol failed this cycle. Don't raise — the loop
                # is responsible for not crashing on transient feed
                # problems — but log loudly so operators see it.
                await logger.awarn(
                    "subscriber.full_batch_failure symbols=%d", len(symbols),
                )
        except Exception:
            await logger.aexception("subscriber.poll_failed")

        # Flush any minutes that completed this cycle. Cheap; the store
        # only writes if something has rolled over.
        try:
            store.flush()
        except Exception:
            await logger.aexception("subscriber.flush_failed")

        await anyio.sleep(poll_interval)
