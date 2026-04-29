"""Market data provider — aggregates quotes from multiple sources (§5.7)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()


class MarketDataProvider:
    """Aggregates market data from the broker adapter (and future sources).

    For v0, delegates entirely to the broker adapter's get_quote.
    Future: yfinance, Google Finance for redundancy/cross-check.
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    async def get_price(self, symbol: str) -> Decimal:
        """Get the current mid price for a symbol."""
        quote = await self._broker.get_quote(symbol)
        price = quote.get("price")
        if isinstance(price, Decimal):
            return price
        return Decimal(str(price))

    async def get_prices(self, symbols: set[str]) -> dict[str, Decimal]:
        """Get current prices for multiple symbols.

        Symbol-level failures don't abort the batch. An unsupported
        ticker (e.g. XSP on Alpaca, which doesn't carry SPX mini
        options), a transient network blip, or a malformed quote
        response on one symbol must not poison every bot watching
        any other symbol. Missing symbols are omitted from the
        returned dict; bots and the risk manager treat
        symbols-without-prices as "no data this tick".
        """

        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            try:
                prices[symbol] = await self.get_price(symbol)
            except Exception:
                logger.warning(
                    "marketdata.provider.quote_failed symbol=%s",
                    symbol,
                    exc_info=True,
                )
                continue
        return prices

    async def get_quote(self, symbol: str) -> dict[str, object]:
        """Get a full quote (bid/ask/mid/sizes) for a symbol."""
        result: dict[str, object] = await self._broker.get_quote(symbol)
        return result
