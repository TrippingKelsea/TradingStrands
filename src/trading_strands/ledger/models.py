"""Ledger data models — per-bot balance sheet and fee tracking (§3.1, §3.4)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Side(StrEnum):
    """Trade side."""

    BUY = "buy"
    SELL = "sell"


class FeeBreakdown(BaseModel):
    """Granular fee breakdown for a single fill (§3.4)."""

    # Commission
    commission: Decimal = Decimal("0")

    # Regulatory
    sec_fee: Decimal = Decimal("0")
    taf_fee: Decimal = Decimal("0")
    finra_fee: Decimal = Decimal("0")

    # Options
    options_per_contract: Decimal = Decimal("0")
    options_exercise: Decimal = Decimal("0")

    # Crypto
    crypto_spread: Decimal = Decimal("0")
    crypto_network: Decimal = Decimal("0")

    # Extensible
    other: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return (
            self.commission
            + self.sec_fee
            + self.taf_fee
            + self.finra_fee
            + self.options_per_contract
            + self.options_exercise
            + self.crypto_spread
            + self.crypto_network
            + self.other
        )


class Fill(BaseModel):
    """A completed fill with fee breakdown."""

    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fees: FeeBreakdown = Field(default_factory=FeeBreakdown)

    @property
    def gross_value(self) -> Decimal:
        """Execution price * quantity, unsigned."""
        return self.price * self.quantity

    @property
    def burdened_total(self) -> Decimal:
        """Total cost (buy) or net proceeds (sell), including fees.

        Buy:  gross + fees (you pay more)
        Sell: gross - fees (you receive less)
        """
        if self.side == Side.BUY:
            return self.gross_value + self.fees.total
        return self.gross_value - self.fees.total

    @property
    def burdened_cost_per_unit(self) -> Decimal:
        """Fully-burdened cost per share/unit."""
        return self.burdened_total / self.quantity


class Position(BaseModel):
    """An open position with fully-burdened cost basis."""

    symbol: str
    quantity: Decimal
    burdened_cost_basis: Decimal  # per unit

    @classmethod
    def from_fill(cls, fill: Fill) -> Position:
        if fill.side != Side.BUY:
            msg = "can only open a position from a BUY fill"
            raise ValueError(msg)
        return cls(
            symbol=fill.symbol,
            quantity=fill.quantity,
            burdened_cost_basis=fill.burdened_cost_per_unit,
        )

    def unrealized_pnl(self, market_price: Decimal) -> Decimal:
        """Unrealized PnL at a given market price."""
        return (market_price - self.burdened_cost_basis) * self.quantity


class Ledger(BaseModel):
    """Per-bot balance sheet (§3.1)."""

    starting_capital: Decimal
    realized_pnl: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")
    open_positions: list[Position] = Field(default_factory=list)
    order_history: list[Fill] = Field(default_factory=list)
    fee_ledger: list[FeeBreakdown] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if self.high_water_mark == Decimal("0"):
            self.high_water_mark = self.starting_capital

    @property
    def equity(self) -> Decimal:
        """Equity based on closed positions only (no mark-to-market)."""
        return self.starting_capital + self.realized_pnl

    def equity_marked(self, market_prices: dict[str, Decimal]) -> Decimal:
        """Equity marked to market with current prices."""
        unrealized = sum(
            pos.unrealized_pnl(market_prices[pos.symbol])
            for pos in self.open_positions
        )
        return self.equity + unrealized

    @property
    def drawdown_from_hwm(self) -> Decimal:
        """Absolute drawdown from high water mark."""
        return self.high_water_mark - self.equity

    @property
    def drawdown_pct(self) -> Decimal:
        """Drawdown as a fraction of high water mark."""
        if self.high_water_mark == 0:
            return Decimal("0")
        return self.drawdown_from_hwm / self.high_water_mark

    def _find_position(self, symbol: str) -> Position | None:
        for pos in self.open_positions:
            if pos.symbol == symbol:
                return pos
        return None

    def record_fill(self, fill: Fill) -> None:
        """Record a fill, updating positions, PnL, fees, and history."""
        self.order_history.append(fill)
        self.fee_ledger.append(fill.fees)

        if fill.side == Side.BUY:
            self._handle_buy(fill)
        else:
            self._handle_sell(fill)

        # Update high water mark
        if self.equity > self.high_water_mark:
            self.high_water_mark = self.equity

    def _handle_buy(self, fill: Fill) -> None:
        existing = self._find_position(fill.symbol)
        if existing is None:
            self.open_positions.append(Position.from_fill(fill))
        else:
            # Average in: weighted average of burdened cost basis
            total_qty = existing.quantity + fill.quantity
            total_cost = (
                existing.burdened_cost_basis * existing.quantity
                + fill.burdened_cost_per_unit * fill.quantity
            )
            existing.burdened_cost_basis = total_cost / total_qty
            existing.quantity = total_qty

    def _handle_sell(self, fill: Fill) -> None:
        pos = self._find_position(fill.symbol)
        if pos is None:
            msg = f"{fill.symbol}: no open position to sell"
            raise ValueError(msg)
        if fill.quantity > pos.quantity:
            msg = f"{fill.symbol}: sell quantity {fill.quantity} exceeds held {pos.quantity}"
            raise ValueError(msg)

        # Realized PnL: (sell proceeds - fees) - (burdened cost basis * qty sold)
        sell_proceeds = fill.burdened_total  # gross - sell fees
        cost_of_sold = pos.burdened_cost_basis * fill.quantity
        self.realized_pnl += sell_proceeds - cost_of_sold

        # Reduce or close position
        remaining = pos.quantity - fill.quantity
        if remaining == 0:
            self.open_positions.remove(pos)
        else:
            pos.quantity = remaining
