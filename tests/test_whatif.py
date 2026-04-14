"""Tests for the what-if counterfactual tracker (§2.1).

Records trades the bot considered but passed on, virtually fills them
at the decision moment's market price, and marks them to market so
the operator can reason about missed opportunities.
"""

from decimal import Decimal

from trading_strands.whatif.tracker import (
    CounterfactualStatus,
    WhatIfTracker,
)


class TestWhatIfTracker:
    def test_record_passed_trade(self) -> None:
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1",
            symbol="AAPL",
            action="buy",
            quantity=Decimal("10"),
            price_at_decision=Decimal("150.00"),
            rationale="considered but RSI too high",
        )
        assert len(tracker.entries) == 1
        entry = tracker.entries[0]
        assert entry.symbol == "AAPL"
        assert entry.price_at_decision == Decimal("150.00")
        assert entry.status == CounterfactualStatus.OPEN

    def test_mark_to_market(self) -> None:
        """Virtually filled entries should track unrealized PnL."""
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1",
            symbol="AAPL",
            action="buy",
            quantity=Decimal("10"),
            price_at_decision=Decimal("150.00"),
            rationale="passed",
        )
        prices = {"AAPL": Decimal("160.00")}
        tracker.mark_to_market(prices)

        entry = tracker.entries[0]
        assert entry.current_price == Decimal("160.00")
        assert entry.unrealized_pnl == Decimal("100.00")  # ($160-$150) * 10

    def test_mark_to_market_losing(self) -> None:
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1",
            symbol="AAPL",
            action="buy",
            quantity=Decimal("10"),
            price_at_decision=Decimal("150.00"),
            rationale="passed",
        )
        prices = {"AAPL": Decimal("140.00")}
        tracker.mark_to_market(prices)

        entry = tracker.entries[0]
        assert entry.unrealized_pnl == Decimal("-100.00")

    def test_sell_counterfactual_pnl(self) -> None:
        """A passed sell means you held — PnL is inverted."""
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1",
            symbol="AAPL",
            action="sell",
            quantity=Decimal("10"),
            price_at_decision=Decimal("150.00"),
            rationale="considered selling but held",
        )
        # Price dropped — not selling was good
        prices = {"AAPL": Decimal("140.00")}
        tracker.mark_to_market(prices)

        entry = tracker.entries[0]
        # If you had sold at $150 and price went to $140, selling would have
        # been +$100 vs holding. So the counterfactual PnL (what you missed)
        # is +$100 (the sell you didn't take).
        assert entry.unrealized_pnl == Decimal("100.00")

    def test_multiple_entries(self) -> None:
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1", symbol="AAPL", action="buy",
            quantity=Decimal("10"), price_at_decision=Decimal("150.00"),
            rationale="r1",
        )
        tracker.record_passed(
            bot_id="bot-1", symbol="MSFT", action="buy",
            quantity=Decimal("5"), price_at_decision=Decimal("400.00"),
            rationale="r2",
        )
        assert len(tracker.entries) == 2

    def test_mark_to_market_missing_symbol(self) -> None:
        """Missing price should not crash — entry stays at last known price."""
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1", symbol="AAPL", action="buy",
            quantity=Decimal("10"), price_at_decision=Decimal("150.00"),
            rationale="passed",
        )
        tracker.mark_to_market({})  # no AAPL price
        entry = tracker.entries[0]
        assert entry.current_price == Decimal("150.00")  # stays at decision price

    def test_summary(self) -> None:
        """Summary should show aggregate what-if PnL."""
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1", symbol="AAPL", action="buy",
            quantity=Decimal("10"), price_at_decision=Decimal("150.00"),
            rationale="r1",
        )
        tracker.record_passed(
            bot_id="bot-1", symbol="MSFT", action="buy",
            quantity=Decimal("5"), price_at_decision=Decimal("400.00"),
            rationale="r2",
        )
        tracker.mark_to_market({"AAPL": Decimal("160.00"), "MSFT": Decimal("390.00")})

        summary = tracker.summary()
        assert summary["total_entries"] == 2
        # AAPL: +$100, MSFT: -$50 = +$50 total
        assert summary["total_unrealized_pnl"] == Decimal("50.00")
        assert summary["best_missed"]["symbol"] == "AAPL"
        assert summary["worst_missed"]["symbol"] == "MSFT"

    def test_filter_by_bot(self) -> None:
        tracker = WhatIfTracker()
        tracker.record_passed(
            bot_id="bot-1", symbol="AAPL", action="buy",
            quantity=Decimal("10"), price_at_decision=Decimal("150.00"),
            rationale="r1",
        )
        tracker.record_passed(
            bot_id="bot-2", symbol="MSFT", action="buy",
            quantity=Decimal("5"), price_at_decision=Decimal("400.00"),
            rationale="r2",
        )
        bot1_entries = tracker.entries_for_bot("bot-1")
        assert len(bot1_entries) == 1
        assert bot1_entries[0].symbol == "AAPL"

    def test_record_taken_action(self) -> None:
        """Should also track the action that was actually taken."""
        tracker = WhatIfTracker()
        tracker.record_taken(
            bot_id="bot-1",
            symbol="AAPL",
            action="buy",
            quantity=Decimal("10"),
            price=Decimal("150.00"),
            rationale="20-day breakout",
        )
        assert len(tracker.taken_actions) == 1
        assert tracker.taken_actions[0].symbol == "AAPL"
