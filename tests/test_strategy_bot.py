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


class TestCalendarContext:
    """_build_calendar_context: the four cases documented above the
    function. These exercise the decision-prompt injection path
    without requiring a full StrategyBot (which would need Bedrock)."""

    def test_no_store_returns_not_wired(self) -> None:
        from trading_strands.strategies.bot import _build_calendar_context

        out = _build_calendar_context(
            calendar_store=None,
            calendar_enabled=True,   # irrelevant when no store
            symbols={"AAPL"},
            bot_id="bot-1",
        )
        assert "(no calendar wired)" in out

    def test_wired_but_disabled(self) -> None:
        from trading_strands.strategies.bot import _build_calendar_context

        class _Store:
            def get_day(self, _date: str) -> None: ...  # never called

        out = _build_calendar_context(
            calendar_store=_Store(),
            calendar_enabled=False,
            symbols={"AAPL"},
            bot_id="bot-1",
        )
        assert "disabled" in out.lower()

    def test_store_read_failure_falls_back(self) -> None:
        """Store read raising must not crash the decision. Returns a
        marker telling the LLM calendar data is unavailable."""

        from trading_strands.strategies.bot import _build_calendar_context

        class _BrokenStore:
            def get_day(self, _date: str) -> None:
                raise RuntimeError("ddb throttled")

        out = _build_calendar_context(
            calendar_store=_BrokenStore(),
            calendar_enabled=True,
            symbols={"AAPL"},
            bot_id="bot-1",
        )
        assert "failed" in out.lower()

    def test_wired_enabled_produces_summary(self) -> None:
        """Happy path: store returns None (no events today/tomorrow)
        so summarize_for_symbols produces the empty-day response.
        Proves the helper actually calls into summarize_for_symbols
        rather than short-circuiting."""

        from trading_strands.strategies.bot import _build_calendar_context

        class _EmptyStore:
            def get_day(self, _date: str) -> None:
                return None  # no row for any date

        out = _build_calendar_context(
            calendar_store=_EmptyStore(),
            calendar_enabled=True,
            symbols={"AAPL"},
            bot_id="bot-1",
        )
        # With both days None, summarize_for_symbols returns the
        # "Calendar data unavailable." marker (§ formatter test).
        assert "unavailable" in out.lower()


class TestTAContext:
    """_build_ta_context: same four-case semantics as calendar."""

    def test_no_store_returns_not_wired(self) -> None:
        from trading_strands.strategies.bot import _build_ta_context

        out = _build_ta_context(
            ta_store=None, ta_enabled=True,
            symbols={"AAPL"}, bot_id="bot-1",
        )
        assert "(no TA wired)" in out

    def test_wired_but_disabled(self) -> None:
        from trading_strands.strategies.bot import _build_ta_context

        class _Store:
            def get_latest(self, _sym: str) -> None: ...

        out = _build_ta_context(
            ta_store=_Store(), ta_enabled=False,
            symbols={"AAPL"}, bot_id="bot-1",
        )
        assert "disabled" in out.lower()

    def test_read_failure_falls_back(self) -> None:
        from trading_strands.strategies.bot import _build_ta_context

        class _BrokenStore:
            def get_latest(self, _sym: str) -> None:
                raise RuntimeError("ddb throttled")

        out = _build_ta_context(
            ta_store=_BrokenStore(), ta_enabled=True,
            symbols={"AAPL"}, bot_id="bot-1",
        )
        assert "failed" in out.lower()

    def test_enabled_renders_symbol_block(self) -> None:
        """Happy path: the formatter produces one line per symbol,
        starting with the symbol name."""

        from trading_strands.strategies.bot import _build_ta_context

        class _EmptyStore:
            def get_latest(self, _sym: str) -> None:
                return None   # no data yet → "(no recent TA snapshot)"

        out = _build_ta_context(
            ta_store=_EmptyStore(), ta_enabled=True,
            symbols={"AAPL"}, bot_id="bot-1",
        )
        assert "AAPL" in out
        assert "no recent" in out.lower()
