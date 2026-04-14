"""Abstract broker adapter interface (§5.5).

All broker implementations must satisfy this protocol, ensuring brokers
are swappable without changes to upstream components.
"""

from __future__ import annotations

from typing import Protocol

from trading_strands.broker.types import (
    AccountInfo,
    BrokerPosition,
    OrderRequest,
    OrderResult,
)
from trading_strands.ledger.models import FeeBreakdown


class BrokerAdapter(Protocol):
    """Common abstract interface for all broker implementations."""

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        """Submit an order and return the result."""
        ...

    async def get_account(self) -> AccountInfo:
        """Get current account state."""
        ...

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all open positions from the broker."""
        ...

    async def get_quote(self, symbol: str) -> dict[str, object]:
        """Get a current quote for a symbol. Shape is broker-specific."""
        ...

    def get_fee_schedule(self) -> FeeBreakdown:
        """Return the fee schedule template for this broker.

        Returns a FeeBreakdown with the *rates* this broker charges,
        which strategy bots can use for pre-trade cost estimation.
        """
        ...

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        """Estimate fees for a hypothetical order before submission."""
        ...
