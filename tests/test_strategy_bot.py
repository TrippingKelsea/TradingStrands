"""Tests for the strategy bot (§5.2).

Tests the deterministic parts of the bot: prompt formatting, action mapping,
decision history. The LLM integration is tested against live APIs.
"""

from decimal import Decimal

from trading_strands.coordinator.types import IntentAction
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.strategies.bot import (
    BotDecision,
    _format_market_data,
    _format_portfolio,
    _map_action,
)


class TestActionMapping:
    def test_buy(self) -> None:
        assert _map_action("buy") == IntentAction.BUY

    def test_sell(self) -> None:
        assert _map_action("sell") == IntentAction.SELL

    def test_close(self) -> None:
        assert _map_action("close") == IntentAction.CLOSE

    def test_hold(self) -> None:
        assert _map_action("hold") == IntentAction.HOLD

    def test_case_insensitive(self) -> None:
        assert _map_action("BUY") == IntentAction.BUY
        assert _map_action("Sell") == IntentAction.SELL

    def test_unknown_defaults_to_hold(self) -> None:
        assert _map_action("yolo") == IntentAction.HOLD


class TestMarketDataFormatting:
    def test_formats_prices(self) -> None:
        prices = {"AAPL": Decimal("150.25"), "MSFT": Decimal("400.50")}
        result = _format_market_data(prices)
        assert "AAPL" in result
        assert "150.25" in result
        assert "MSFT" in result

    def test_empty_prices(self) -> None:
        result = _format_market_data({})
        assert "No market data" in result


class TestPortfolioFormatting:
    def test_formats_empty_portfolio(self) -> None:
        ledger = Ledger(starting_capital=Decimal("10000"))
        result = _format_portfolio(ledger)
        assert "10000" in result
        assert "No open positions" in result

    def test_formats_with_positions(self) -> None:
        ledger = Ledger(starting_capital=Decimal("10000"))
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        result = _format_portfolio(ledger)
        assert "AAPL" in result
        assert "10" in result

    def test_formats_drawdown(self) -> None:
        ledger = Ledger(starting_capital=Decimal("10000"))
        ledger.high_water_mark = Decimal("12000")
        result = _format_portfolio(ledger)
        assert "16.67%" in result


class TestBotDecision:
    def test_structured_output_model(self) -> None:
        """BotDecision should be a valid pydantic model for structured output."""
        decision = BotDecision(
            action="buy",
            symbol="AAPL",
            quantity="10",
            rationale="20-day breakout",
        )
        assert decision.action == "buy"
        assert decision.symbol == "AAPL"
        assert decision.quantity == "10"
