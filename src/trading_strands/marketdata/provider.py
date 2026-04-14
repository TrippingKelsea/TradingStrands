"""Market data provider — aggregates quotes from multiple sources (§5.7)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


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
        """Get current prices for multiple symbols."""
        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            prices[symbol] = await self.get_price(symbol)
        return prices

    async def get_quote(self, symbol: str) -> dict[str, object]:
        """Get a full quote (bid/ask/mid/sizes) for a symbol."""
        result: dict[str, object] = await self._broker.get_quote(symbol)
        return result
