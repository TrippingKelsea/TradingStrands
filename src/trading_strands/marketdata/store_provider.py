"""DDB-backed MarketDataProvider.

Reads prices from MarketDataStore instead of calling the broker on
every tick. Falls back to the broker when:
  - the symbol has no bars in the store yet (new symbol, subscriber
    hasn't caught up);
  - the most recent bar is older than `staleness_threshold_seconds`
    (subscriber lagging or offline).

The fallback is deliberate: a strategy task with a missing price
would produce bad trade sizing or skip a signal entirely. Reading
from the broker is slower and makes more API calls, but never
silently wrong.

Opt in at construction time; pick which MarketDataProvider to
instantiate in app.py via env flag. Swap is reversible — the
provider shape matches the broker-backed version exactly.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from trading_strands.marketdata_store.store import (
    MarketDataStore,
    hour_bucket,
)

logger = structlog.get_logger()


DEFAULT_STALENESS_THRESHOLD_SECONDS = 120.0


class StoreBackedMarketDataProvider:
    """MarketDataProvider that prefers MarketDataStore, falls back to
    the broker. Interchangeable with the broker-only provider in
    marketdata/provider.py — same async methods, same return types.
    """

    def __init__(
        self,
        store: MarketDataStore,
        fallback_broker: Any,
        staleness_threshold_seconds: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
    ) -> None:
        self._store = store
        self._broker = fallback_broker
        self._staleness = staleness_threshold_seconds

    def _read_fresh_from_store(self, symbol: str) -> Decimal | None:
        """Return the most-recent price from the store if it is within
        the staleness threshold; None otherwise. Synchronous — the
        store is already a local DDB read, no async needed inside."""

        now = time.time()
        bucket = hour_bucket(now)
        bars = self._store.get_hour(symbol, bucket)

        if not bars:
            # Hour just rolled over and the subscriber hasn't written
            # anything for this hour yet — peek at the previous hour.
            prev_bucket = hour_bucket(now - 3600)
            bars = self._store.get_hour(symbol, prev_bucket)
            if not bars:
                return None

        # Find the bar with the newest last_ts.
        best_ts = 0
        best_bar: dict[str, Any] | None = None
        for bar in bars.values():
            ts = int(bar.get("last_ts", 0))
            if ts > best_ts:
                best_ts = ts
                best_bar = bar
        if best_bar is None:
            return None
        if (now - best_ts) > self._staleness:
            return None
        close_str = str(best_bar.get("close", ""))
        if not close_str:
            return None
        return Decimal(close_str)

    async def get_price(self, symbol: str) -> Decimal:
        """Price for a single symbol. Prefers the store; falls back
        to the broker on a miss or staleness."""

        cached = self._read_fresh_from_store(symbol)
        if cached is not None:
            return cached

        # Store miss — fall back. Broker errors propagate; a strategy
        # that can't price a symbol must not silently proceed.
        logger.debug(
            "store_provider.fallback symbol=%s reason=miss_or_stale",
            symbol,
        )
        quote = await self._broker.get_quote(symbol)
        price = quote.get("price")
        if isinstance(price, Decimal):
            return price
        return Decimal(str(price))

    async def get_prices(self, symbols: set[str]) -> dict[str, Decimal]:
        """Batch read. Each symbol independently hits the store; misses
        go to the broker.

        Symbol-level failures don't abort the batch. An Alpaca KeyError
        (unsupported ticker), a network blip on a single symbol, or a
        malformed quote response for one name must not poison every
        bot that watches any other symbol. Missing symbols are omitted
        from the returned dict — downstream consumers (bots, risk
        manager) already treat symbols-without-prices as "no data this
        tick" rather than crashing.
        """

        prices: dict[str, Decimal] = {}
        misses: list[str] = []
        for sym in symbols:
            cached = self._read_fresh_from_store(sym)
            if cached is not None:
                prices[sym] = cached
            else:
                misses.append(sym)
        for sym in misses:
            try:
                quote = await self._broker.get_quote(sym)
            except Exception as exc:
                # No traceback — repeated unsupported-symbol errors
                # would drown out every other log line otherwise.
                logger.warning(
                    "store_provider.quote_failed symbol=%s err=%s",
                    sym,
                    f"{type(exc).__name__}: {exc}",
                )
                continue
            price = quote.get("price")
            if price is None:
                # Broker returned a malformed quote; skip cleanly.
                continue
            try:
                prices[sym] = (
                    price if isinstance(price, Decimal)
                    else Decimal(str(price))
                )
            except (InvalidOperation, ValueError):
                logger.warning(
                    "store_provider.price_unparseable symbol=%s price=%r",
                    sym, price,
                )
                continue
        return prices

    async def get_quote(self, symbol: str) -> dict[str, object]:
        """Full quotes aren't captured in MarketDataStore's minute-bar
        schema — always go to the broker. The price fastpath is the
        only thing this provider optimizes."""

        result: dict[str, object] = await self._broker.get_quote(symbol)
        return result
