"""Types for the trade coordinator."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from trading_strands.ledger.models import Side


class IntentAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


class TradeIntent(BaseModel):
    """A trade intent emitted by a strategy bot (§4.3)."""

    bot_id: str
    symbol: str
    action: IntentAction
    quantity: Decimal
    rationale: str = ""


class RiskVerdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskDecision(BaseModel):
    """Result of a risk manager evaluation."""

    verdict: RiskVerdict
    intent: TradeIntent
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.verdict == RiskVerdict.APPROVED


def intent_to_side(action: IntentAction) -> Side:
    """Map an intent action to a trade side."""
    if action in (IntentAction.SELL, IntentAction.CLOSE):
        return Side.SELL
    return Side.BUY
