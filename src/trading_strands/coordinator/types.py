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
    """A trade intent emitted by a strategy bot (§4.3).

    `org_id` is required because the coordinator uses it to route the trade
    to the correct per-org broker. A strategy in org A must never execute
    through org B's broker credentials; that invariant is enforced by
    having the intent carry the scope rather than being inferred.
    """

    bot_id: str
    org_id: str
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
