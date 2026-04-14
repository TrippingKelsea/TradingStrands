"""Shared types for the broker layer."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from trading_strands.ledger.models import FeeBreakdown, Side


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class OrderRequest(BaseModel):
    """Canonical order format submitted to the broker adapter."""

    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY


class OrderResult(BaseModel):
    """Result of an order submission."""

    order_id: str
    status: OrderStatus
    filled_quantity: Decimal = Decimal("0")
    filled_price: Decimal = Decimal("0")
    fees: FeeBreakdown = Field(default_factory=FeeBreakdown)
    reject_reason: str | None = None


class AccountInfo(BaseModel):
    """Broker account state snapshot."""

    cash: Decimal
    portfolio_value: Decimal
    buying_power: Decimal


class BrokerPosition(BaseModel):
    """A position as reported by the broker."""

    symbol: str
    quantity: Decimal
    market_value: Decimal
    current_price: Decimal
