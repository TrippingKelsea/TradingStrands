"""Tests for the per-bot ledger and fee tracking (§3.1, §3.4)."""

from decimal import Decimal

import pytest

from trading_strands.ledger.models import (
    FeeBreakdown,
    Fill,
    Ledger,
    Position,
    Side,
)

# ── Fee breakdown ──


class TestFeeBreakdown:
    def test_total_sums_all_components(self) -> None:
        fees = FeeBreakdown(
            commission=Decimal("1.00"),
            sec_fee=Decimal("0.03"),
            taf_fee=Decimal("0.01"),
            finra_fee=Decimal("0.02"),
        )
        assert fees.total == Decimal("1.06")

    def test_zero_fees(self) -> None:
        fees = FeeBreakdown()
        assert fees.total == Decimal("0")

    def test_options_and_crypto_fees(self) -> None:
        fees = FeeBreakdown(
            options_per_contract=Decimal("0.65"),
            options_exercise=Decimal("15.00"),
            crypto_spread=Decimal("2.50"),
            crypto_network=Decimal("1.00"),
        )
        assert fees.total == Decimal("19.15")

    def test_other_fees_included_in_total(self) -> None:
        fees = FeeBreakdown(
            commission=Decimal("1.00"),
            other=Decimal("0.50"),
        )
        assert fees.total == Decimal("1.50")


# ── Fill ──


class TestFill:
    def test_burdened_cost_buy(self) -> None:
        """Buying 10 shares at $50 with $1.06 in fees = $501.06 total cost."""
        fees = FeeBreakdown(
            commission=Decimal("1.00"),
            sec_fee=Decimal("0.03"),
            taf_fee=Decimal("0.01"),
            finra_fee=Decimal("0.02"),
        )
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("50.00"),
            fees=fees,
        )
        assert fill.burdened_total == Decimal("501.06")

    def test_burdened_cost_sell(self) -> None:
        """Selling 10 shares at $55 with $1.06 in fees = $548.94 net proceeds."""
        fees = FeeBreakdown(
            commission=Decimal("1.00"),
            sec_fee=Decimal("0.03"),
            taf_fee=Decimal("0.01"),
            finra_fee=Decimal("0.02"),
        )
        fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("55.00"),
            fees=fees,
        )
        assert fill.burdened_total == Decimal("548.94")

    def test_burdened_cost_per_share_buy(self) -> None:
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("50.00"),
            fees=fees,
        )
        # $501 / 10 = $50.10 per share
        assert fill.burdened_cost_per_unit == Decimal("50.10")


# ── Position ──


class TestPosition:
    def test_open_position_from_fill(self) -> None:
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("50.00"),
            fees=fees,
        )
        pos = Position.from_fill(fill)
        assert pos.symbol == "AAPL"
        assert pos.quantity == Decimal("10")
        assert pos.burdened_cost_basis == Decimal("50.10")  # per share

    def test_unrealized_pnl(self) -> None:
        """Position bought at $50.10 burdened, marked at $55 = $4.90/share * 10."""
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("50.00"),
            fees=fees,
        )
        pos = Position.from_fill(fill)
        assert pos.unrealized_pnl(Decimal("55.00")) == Decimal("49.00")

    def test_unrealized_pnl_losing(self) -> None:
        """Position bought at $50.10 burdened, marked at $48 = -$2.10/share * 10."""
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("50.00"),
            fees=fees,
        )
        pos = Position.from_fill(fill)
        assert pos.unrealized_pnl(Decimal("48.00")) == Decimal("-21.00")


# ── Ledger ──


class TestLedger:
    def _make_ledger(self, capital: str = "10000") -> Ledger:
        return Ledger(starting_capital=Decimal(capital))

    def test_initial_state(self) -> None:
        ledger = self._make_ledger()
        assert ledger.equity == Decimal("10000")
        assert ledger.realized_pnl == Decimal("0")
        assert ledger.high_water_mark == Decimal("10000")
        assert len(ledger.open_positions) == 0
        assert len(ledger.order_history) == 0
        assert len(ledger.fee_ledger) == 0

    def test_record_buy_fill(self) -> None:
        ledger = self._make_ledger()
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=fees,
        )
        ledger.record_fill(fill)
        assert len(ledger.open_positions) == 1
        assert len(ledger.order_history) == 1
        assert len(ledger.fee_ledger) == 1
        assert ledger.fee_ledger[0] is fees

    def test_equity_after_buy_at_cost(self) -> None:
        """Equity should reflect the position at market price. If market price
        equals entry price, equity drops only by fees."""
        ledger = self._make_ledger()
        fees = FeeBreakdown(commission=Decimal("1.00"))
        fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=fees,
        )
        ledger.record_fill(fill)
        # Mark to market at entry price: equity = 10000 - 1.00 fee drag
        market_prices = {"AAPL": Decimal("100.00")}
        assert ledger.equity_marked(market_prices) == Decimal("9999.00")

    def test_close_position_realized_pnl(self) -> None:
        """Buy 10 at $100 ($1 commission), sell 10 at $110 ($1 commission).
        Gross profit = $100, fees = $2, realized PnL = $98."""
        ledger = self._make_ledger()
        buy_fees = FeeBreakdown(commission=Decimal("1.00"))
        buy_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=buy_fees,
        )
        ledger.record_fill(buy_fill)

        sell_fees = FeeBreakdown(commission=Decimal("1.00"))
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("110.00"),
            fees=sell_fees,
        )
        ledger.record_fill(sell_fill)

        assert ledger.realized_pnl == Decimal("98.00")
        assert len(ledger.open_positions) == 0
        assert ledger.equity == Decimal("10098.00")

    def test_close_position_losing_trade(self) -> None:
        """Buy 10 at $100 ($1 commission), sell 10 at $95 ($1 commission).
        Gross loss = -$50, fees = $2, realized PnL = -$52."""
        ledger = self._make_ledger()
        buy_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(commission=Decimal("1.00")),
        )
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("95.00"),
            fees=FeeBreakdown(commission=Decimal("1.00")),
        )
        ledger.record_fill(buy_fill)
        ledger.record_fill(sell_fill)

        assert ledger.realized_pnl == Decimal("-52.00")
        assert ledger.equity == Decimal("9948.00")

    def test_high_water_mark_updates(self) -> None:
        ledger = self._make_ledger()
        buy_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        )
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("120.00"),
            fees=FeeBreakdown(),
        )
        ledger.record_fill(buy_fill)
        ledger.record_fill(sell_fill)

        assert ledger.high_water_mark == Decimal("10200.00")

    def test_partial_close(self) -> None:
        """Buy 10, sell 5 — should have 5 remaining open."""
        ledger = self._make_ledger()
        buy_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(commission=Decimal("1.00")),
        )
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("5"),
            price=Decimal("110.00"),
            fees=FeeBreakdown(commission=Decimal("1.00")),
        )
        ledger.record_fill(buy_fill)
        ledger.record_fill(sell_fill)

        assert len(ledger.open_positions) == 1
        assert ledger.open_positions[0].quantity == Decimal("5")

    def test_sell_without_position_raises(self) -> None:
        ledger = self._make_ledger()
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        )
        with pytest.raises(ValueError, match="no open position"):
            ledger.record_fill(sell_fill)

    def test_sell_more_than_held_raises(self) -> None:
        ledger = self._make_ledger()
        buy_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("5"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        )
        sell_fill = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        )
        ledger.record_fill(buy_fill)
        with pytest.raises(ValueError, match="exceeds"):
            ledger.record_fill(sell_fill)

    def test_total_fees(self) -> None:
        """Fee ledger should accumulate all fees across fills."""
        ledger = self._make_ledger()
        fill1 = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(commission=Decimal("1.00"), sec_fee=Decimal("0.03")),
        )
        fill2 = Fill(
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("105.00"),
            fees=FeeBreakdown(commission=Decimal("1.00"), taf_fee=Decimal("0.01")),
        )
        ledger.record_fill(fill1)
        ledger.record_fill(fill2)

        total_fees = sum(f.total for f in ledger.fee_ledger)
        assert total_fees == Decimal("2.04")

    def test_multiple_symbols(self) -> None:
        """Ledger should track positions in different symbols independently."""
        ledger = self._make_ledger()
        aapl_fill = Fill(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        )
        msft_fill = Fill(
            symbol="MSFT",
            side=Side.BUY,
            quantity=Decimal("5"),
            price=Decimal("200.00"),
            fees=FeeBreakdown(),
        )
        ledger.record_fill(aapl_fill)
        ledger.record_fill(msft_fill)

        assert len(ledger.open_positions) == 2
        symbols = {p.symbol for p in ledger.open_positions}
        assert symbols == {"AAPL", "MSFT"}

    def test_drawdown_from_hwm(self) -> None:
        """Drawdown should be measured from high water mark."""
        ledger = self._make_ledger("10000")
        # Win a trade to push HWM up
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.SELL, quantity=Decimal("10"),
            price=Decimal("120.00"), fees=FeeBreakdown(),
        ))
        assert ledger.high_water_mark == Decimal("10200.00")

        # Lose a trade
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.SELL, quantity=Decimal("10"),
            price=Decimal("80.00"), fees=FeeBreakdown(),
        ))
        # Equity = 10200 - 200 = 10000, HWM still 10200
        assert ledger.equity == Decimal("10000.00")
        assert ledger.high_water_mark == Decimal("10200.00")
        assert ledger.drawdown_from_hwm == Decimal("200.00")
        assert ledger.drawdown_pct == pytest.approx(Decimal("200") / Decimal("10200"))
