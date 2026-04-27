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
        """HOLD = 'maintain' — active decision to keep the existing
        position shape."""

        assert _map_action("hold") == IntentAction.HOLD

    def test_noop(self) -> None:
        """NOOP = 'stand_down' — explicit decline to engage. Distinct
        from HOLD: noop means no position and no signal worth acting
        on; hold means a position exists and is being deliberately
        kept."""

        assert _map_action("noop") == IntentAction.NOOP

    def test_case_insensitive(self) -> None:
        assert _map_action("BUY") == IntentAction.BUY
        assert _map_action("Sell") == IntentAction.SELL
        assert _map_action("NOOP") == IntentAction.NOOP

    def test_unknown_defaults_to_noop(self) -> None:
        """Unknown / garbled action string → NOOP (stand_down).
        Changed from HOLD default because unknown input ≈ "bot is
        confused"; the safer posture is to step away, not to imply
        we're deliberately holding a position we can't reason about."""

        assert _map_action("yolo") == IntentAction.NOOP
        assert _map_action("") == IntentAction.NOOP


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


class TestMemoryLineFormat:
    """_format_memory_line: per docs/SPEC/agent_memory.md §"Anti-
    confabulation rule" every memory line must carry a MARKETDATA#
    pointer so a reviewer can dereference the claim."""

    def _fake_gmtime(
        self, hour: int = 15, minute: int = 47, second: int = 3,
    ) -> object:
        from types import SimpleNamespace
        return SimpleNamespace(
            tm_year=2026, tm_mon=4, tm_mday=26,
            tm_hour=hour, tm_min=minute, tm_sec=second,
        )

    def test_includes_marketdata_pointer(self) -> None:
        from trading_strands.strategies.bot import _format_memory_line

        decision = BotDecision(
            action="buy", symbol="SPY", quantity="10",
            rationale="RSI cross on 15m",
        )
        ledger = Ledger(starting_capital=Decimal("10000"))
        line = _format_memory_line(
            decision,
            prices={"SPY": Decimal("521.43")},
            ledger=ledger,
            now_gmtime=self._fake_gmtime(hour=15, minute=47, second=3),
        )
        # MARKETDATA pointer with hourly bucket — auditors dereference
        # this to confirm the price and reasoning actually line up
        # with observed market state at the tick.
        assert "[MARKETDATA#SPY#2026042615]" in line
        assert "BUY SPY x10" in line
        assert "521.43" in line
        assert "15:47:03Z" in line

    def test_missing_price_still_has_pointer(self) -> None:
        """If the symbol isn't in the price dict the line still carries
        a pointer — the reviewer dereferencing it will see "no data for
        this bucket" and flag the confabulation, which is the intended
        audit signal. Dropping the pointer would hide the gap."""

        from trading_strands.strategies.bot import _format_memory_line

        decision = BotDecision(
            action="noop", symbol="TSLA", quantity="0",
            rationale="stand down",
        )
        ledger = Ledger(starting_capital=Decimal("10000"))
        line = _format_memory_line(
            decision, prices={}, ledger=ledger,
            now_gmtime=self._fake_gmtime(),
        )
        assert "[MARKETDATA#TSLA#2026042615]" in line
        assert "~?" in line


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
