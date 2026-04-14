"""What-if counterfactual tracker (§2.1).

Records trades the bot considered but passed on, virtually fills them
at the decision moment's market price, and marks them to market
continuously. This lets the operator reason about missed opportunities
using real market data.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class CounterfactualStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CounterfactualEntry(BaseModel):
    """A trade that was considered but not taken."""

    bot_id: str
    symbol: str
    action: str  # buy or sell
    quantity: Decimal
    price_at_decision: Decimal
    current_price: Decimal = Decimal("0")
    rationale: str = ""
    status: CounterfactualStatus = CounterfactualStatus.OPEN

    def model_post_init(self, __context: object) -> None:
        if self.current_price == 0:
            self.current_price = self.price_at_decision

    @property
    def unrealized_pnl(self) -> Decimal:
        """What-if PnL if this trade had been taken.

        Buy: (current - entry) * qty  — you missed gains if price went up
        Sell: (entry - current) * qty  — you missed locking in if price went down
        """
        if self.action == "buy":
            return (self.current_price - self.price_at_decision) * self.quantity
        # Sell: PnL of the sell you didn't take
        return (self.price_at_decision - self.current_price) * self.quantity


class TakenAction(BaseModel):
    """A trade that was actually executed — recorded for context."""

    bot_id: str
    symbol: str
    action: str
    quantity: Decimal
    price: Decimal
    rationale: str = ""


class WhatIfTracker:
    """Tracks counterfactual trades and marks them to market."""

    def __init__(self) -> None:
        self.entries: list[CounterfactualEntry] = []
        self.taken_actions: list[TakenAction] = []

    def record_passed(
        self,
        bot_id: str,
        symbol: str,
        action: str,
        quantity: Decimal,
        price_at_decision: Decimal,
        rationale: str = "",
    ) -> None:
        """Record a trade the bot considered but passed on."""
        self.entries.append(
            CounterfactualEntry(
                bot_id=bot_id,
                symbol=symbol,
                action=action,
                quantity=quantity,
                price_at_decision=price_at_decision,
                rationale=rationale,
            )
        )

    def record_taken(
        self,
        bot_id: str,
        symbol: str,
        action: str,
        quantity: Decimal,
        price: Decimal,
        rationale: str = "",
    ) -> None:
        """Record a trade that was actually taken."""
        self.taken_actions.append(
            TakenAction(
                bot_id=bot_id,
                symbol=symbol,
                action=action,
                quantity=quantity,
                price=price,
                rationale=rationale,
            )
        )

    def mark_to_market(self, prices: dict[str, Decimal]) -> None:
        """Update all open entries with current market prices."""
        for entry in self.entries:
            if entry.status == CounterfactualStatus.OPEN and entry.symbol in prices:
                entry.current_price = prices[entry.symbol]

    def entries_for_bot(self, bot_id: str) -> list[CounterfactualEntry]:
        """Get all counterfactual entries for a specific bot."""
        return [e for e in self.entries if e.bot_id == bot_id]

    def summary(self) -> dict[str, Any]:
        """Aggregate what-if statistics."""
        if not self.entries:
            return {
                "total_entries": 0,
                "total_unrealized_pnl": Decimal("0"),
                "best_missed": None,
                "worst_missed": None,
            }

        total_pnl = sum(e.unrealized_pnl for e in self.entries)
        best = max(self.entries, key=lambda e: e.unrealized_pnl)
        worst = min(self.entries, key=lambda e: e.unrealized_pnl)

        return {
            "total_entries": len(self.entries),
            "total_unrealized_pnl": total_pnl,
            "best_missed": {
                "symbol": best.symbol,
                "pnl": best.unrealized_pnl,
                "rationale": best.rationale,
            },
            "worst_missed": {
                "symbol": worst.symbol,
                "pnl": worst.unrealized_pnl,
                "rationale": worst.rationale,
            },
        }
