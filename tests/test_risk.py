"""Tests for the deterministic risk manager (§5.4)."""

from decimal import Decimal

from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.risk.manager import RiskConfig, RiskManager


def _make_intent(
    symbol: str = "AAPL",
    action: IntentAction = IntentAction.BUY,
    quantity: str = "10",
    bot_id: str = "bot-1",
) -> TradeIntent:
    return TradeIntent(
        bot_id=bot_id,
        symbol=symbol,
        action=action,
        quantity=Decimal(quantity),
    )


def _make_ledger(capital: str = "10000") -> Ledger:
    return Ledger(starting_capital=Decimal(capital))


class TestRiskManager:
    def test_approve_simple_trade(self) -> None:
        """A small trade within all limits should be approved."""
        config = RiskConfig()
        rm = RiskManager(config)
        ledger = _make_ledger()
        intent = _make_intent(quantity="10")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert decision.approved

    def test_reject_exceeds_per_position_limit(self) -> None:
        """A trade that uses more than max_position_pct of equity should be rejected."""
        config = RiskConfig(max_position_pct=Decimal("0.10"))  # 10% max
        rm = RiskManager(config)
        ledger = _make_ledger("10000")
        # 20 shares at $100 = $2000 = 20% of equity
        intent = _make_intent(quantity="20")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "position" in decision.reason.lower()

    def test_reject_exceeds_total_exposure(self) -> None:
        """Total exposure across all positions should be limited."""
        config = RiskConfig(max_total_exposure_pct=Decimal("0.50"))
        rm = RiskManager(config)
        ledger = _make_ledger("10000")

        # Already holding $4000 worth of MSFT
        ledger.record_fill(Fill(
            symbol="MSFT",
            side=Side.BUY,
            quantity=Decimal("40"),
            price=Decimal("100.00"),
            fees=FeeBreakdown(),
        ))

        # Trying to buy $2000 more of AAPL = total $6000 = 60% > 50%
        intent = _make_intent(symbol="AAPL", quantity="20")
        market_prices = {"AAPL": Decimal("100.00"), "MSFT": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "exposure" in decision.reason.lower()

    def test_reject_drawdown_breached(self) -> None:
        """No new buys when drawdown exceeds threshold."""
        config = RiskConfig(max_drawdown_pct=Decimal("0.10"))
        rm = RiskManager(config)
        ledger = _make_ledger("10000")

        # Lose enough to breach 10% drawdown
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("100"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.SELL, quantity=Decimal("100"),
            price=Decimal("88.00"), fees=FeeBreakdown(),
        ))
        # Lost $1200, HWM = $10000, equity = $8800, drawdown = 12%

        intent = _make_intent(symbol="AAPL", quantity="5")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "drawdown" in decision.reason.lower()

    def test_allow_sell_during_drawdown(self) -> None:
        """Sells should be allowed even when drawdown limit is breached."""
        config = RiskConfig(max_drawdown_pct=Decimal("0.10"))
        rm = RiskManager(config)
        ledger = _make_ledger("10000")

        # Open a position, then take a loss to breach drawdown
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("100"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.SELL, quantity=Decimal("100"),
            price=Decimal("88.00"), fees=FeeBreakdown(),
        ))

        # Should still be able to sell the AAPL position
        intent = _make_intent(symbol="AAPL", action=IntentAction.SELL, quantity="10")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert decision.approved

    def test_hold_always_approved(self) -> None:
        """HOLD intents are always approved (no-op)."""
        config = RiskConfig()
        rm = RiskManager(config)
        ledger = _make_ledger()
        intent = _make_intent(action=IntentAction.HOLD, quantity="0")
        market_prices: dict[str, Decimal] = {}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert decision.approved

    def test_reject_daily_loss_cap(self) -> None:
        """No new buys when daily losses exceed the daily loss cap."""
        config = RiskConfig(daily_loss_cap_pct=Decimal("0.05"))
        rm = RiskManager(config)
        ledger = _make_ledger("10000")

        # Lose $600 = 6% of starting equity
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("60"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.SELL, quantity=Decimal("60"),
            price=Decimal("90.00"), fees=FeeBreakdown(),
        ))

        rm.record_daily_loss(Decimal("600.00"))

        intent = _make_intent(symbol="AAPL", quantity="5")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "daily" in decision.reason.lower()

    def test_halted_bot_rejected(self) -> None:
        """A halted bot's intents are always rejected."""
        config = RiskConfig()
        rm = RiskManager(config)
        ledger = _make_ledger()
        rm.halt_bot("bot-1")

        intent = _make_intent(bot_id="bot-1")
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "halted" in decision.reason.lower()

    def test_desk_halted_all_rejected(self) -> None:
        """When the desk is halted, all intents are rejected."""
        config = RiskConfig()
        rm = RiskManager(config)
        ledger = _make_ledger()
        rm.halt_desk()

        intent = _make_intent()
        market_prices = {"AAPL": Decimal("100.00")}

        decision = rm.evaluate(intent, ledger, market_prices)
        assert not decision.approved
        assert "desk" in decision.reason.lower()
