"""Tests for TTA predicate evaluation (§4.1).

TTA predicates are JSON-expressible predicate trees evaluated deterministically
by the orchestrator. They define when a strategy bot should be woken.
"""

from decimal import Decimal

import pytest

from trading_strands.ir.tta import Predicate, evaluate


class TestComparisons:
    def test_gt(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "gt", "value": 150}
        ctx = {"price.AAPL": Decimal("155")}
        assert evaluate(pred, ctx) is True

    def test_gt_false(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "gt", "value": 150}
        ctx = {"price.AAPL": Decimal("145")}
        assert evaluate(pred, ctx) is False

    def test_lt(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "lt", "value": 100}
        ctx = {"price.AAPL": Decimal("95")}
        assert evaluate(pred, ctx) is True

    def test_gte(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "gte", "value": 150}
        ctx = {"price.AAPL": Decimal("150")}
        assert evaluate(pred, ctx) is True

    def test_lte(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "lte", "value": 150}
        ctx = {"price.AAPL": Decimal("150")}
        assert evaluate(pred, ctx) is True

    def test_eq(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "eq", "value": 150}
        ctx = {"price.AAPL": Decimal("150")}
        assert evaluate(pred, ctx) is True

    def test_missing_field_is_false(self) -> None:
        pred: Predicate = {"field": "price.MSFT", "op": "gt", "value": 100}
        ctx = {"price.AAPL": Decimal("155")}
        assert evaluate(pred, ctx) is False


class TestLogicalOperators:
    def test_and_all_true(self) -> None:
        pred: Predicate = {
            "and": [
                {"field": "price.AAPL", "op": "gt", "value": 150},
                {"field": "price.MSFT", "op": "gt", "value": 400},
            ],
        }
        ctx = {"price.AAPL": Decimal("155"), "price.MSFT": Decimal("410")}
        assert evaluate(pred, ctx) is True

    def test_and_one_false(self) -> None:
        pred: Predicate = {
            "and": [
                {"field": "price.AAPL", "op": "gt", "value": 150},
                {"field": "price.MSFT", "op": "gt", "value": 400},
            ],
        }
        ctx = {"price.AAPL": Decimal("155"), "price.MSFT": Decimal("390")}
        assert evaluate(pred, ctx) is False

    def test_or_one_true(self) -> None:
        pred: Predicate = {
            "or": [
                {"field": "price.AAPL", "op": "gt", "value": 200},
                {"field": "price.MSFT", "op": "lt", "value": 300},
            ],
        }
        ctx = {"price.AAPL": Decimal("155"), "price.MSFT": Decimal("290")}
        assert evaluate(pred, ctx) is True

    def test_or_none_true(self) -> None:
        pred: Predicate = {
            "or": [
                {"field": "price.AAPL", "op": "gt", "value": 200},
                {"field": "price.MSFT", "op": "lt", "value": 300},
            ],
        }
        ctx = {"price.AAPL": Decimal("155"), "price.MSFT": Decimal("310")}
        assert evaluate(pred, ctx) is False

    def test_not(self) -> None:
        pred: Predicate = {
            "not": {"field": "price.AAPL", "op": "gt", "value": 200},
        }
        ctx = {"price.AAPL": Decimal("155")}
        assert evaluate(pred, ctx) is True

    def test_nested_logic(self) -> None:
        """(AAPL > 150 AND MSFT > 400) OR drawdown < 0.10"""
        pred: Predicate = {
            "or": [
                {
                    "and": [
                        {"field": "price.AAPL", "op": "gt", "value": 150},
                        {"field": "price.MSFT", "op": "gt", "value": 400},
                    ],
                },
                {"field": "ledger.drawdown_pct", "op": "lt", "value": 0.10},
            ],
        }
        # First branch false (MSFT too low), second branch true
        ctx = {
            "price.AAPL": Decimal("155"),
            "price.MSFT": Decimal("390"),
            "ledger.drawdown_pct": Decimal("0.05"),
        }
        assert evaluate(pred, ctx) is True


class TestCrossPredicates:
    def test_cross_above(self) -> None:
        """Price crossed above threshold: was below, now above."""
        pred: Predicate = {
            "cross": "above",
            "field": "price.AAPL",
            "value": 150,
        }
        prev_ctx = {"price.AAPL": Decimal("148")}
        curr_ctx = {"price.AAPL": Decimal("152")}
        assert evaluate(pred, curr_ctx, prev_ctx) is True

    def test_cross_above_already_above(self) -> None:
        """No crossing if already above."""
        pred: Predicate = {
            "cross": "above",
            "field": "price.AAPL",
            "value": 150,
        }
        prev_ctx = {"price.AAPL": Decimal("151")}
        curr_ctx = {"price.AAPL": Decimal("155")}
        assert evaluate(pred, curr_ctx, prev_ctx) is False

    def test_cross_below(self) -> None:
        """Price crossed below threshold: was above, now below."""
        pred: Predicate = {
            "cross": "below",
            "field": "price.AAPL",
            "value": 100,
        }
        prev_ctx = {"price.AAPL": Decimal("102")}
        curr_ctx = {"price.AAPL": Decimal("98")}
        assert evaluate(pred, curr_ctx, prev_ctx) is True

    def test_cross_no_previous_context(self) -> None:
        """First tick (no previous context) — cross is false."""
        pred: Predicate = {
            "cross": "above",
            "field": "price.AAPL",
            "value": 150,
        }
        ctx = {"price.AAPL": Decimal("155")}
        assert evaluate(pred, ctx) is False


class TestTurtleTradingPredicates:
    """Test predicates that match the turtle trading example strategy."""

    def test_entry_breakout(self) -> None:
        """Buy when price breaks above 20-day high."""
        pred: Predicate = {
            "cross": "above",
            "field": "price.AAPL",
            "value": 155,  # the 20-day high
        }
        prev = {"price.AAPL": Decimal("154")}
        curr = {"price.AAPL": Decimal("156")}
        assert evaluate(pred, curr, prev) is True

    def test_exit_breakdown(self) -> None:
        """Sell when price breaks below 10-day low."""
        pred: Predicate = {
            "cross": "below",
            "field": "price.AAPL",
            "value": 148,  # the 10-day low
        }
        prev = {"price.AAPL": Decimal("149")}
        curr = {"price.AAPL": Decimal("147")}
        assert evaluate(pred, curr, prev) is True

    def test_drawdown_halt(self) -> None:
        """Stop opening positions at 10% drawdown."""
        pred: Predicate = {
            "field": "ledger.drawdown_pct",
            "op": "gte",
            "value": 0.10,
        }
        ctx = {"ledger.drawdown_pct": Decimal("0.12")}
        assert evaluate(pred, ctx) is True


class TestInvalidPredicates:
    def test_unknown_op_raises(self) -> None:
        pred: Predicate = {"field": "price.AAPL", "op": "xor", "value": 100}
        ctx = {"price.AAPL": Decimal("100")}
        with pytest.raises(ValueError, match="unknown operator"):
            evaluate(pred, ctx)

    def test_unknown_cross_direction_raises(self) -> None:
        pred: Predicate = {
            "cross": "sideways",
            "field": "price.AAPL",
            "value": 100,
        }
        prev = {"price.AAPL": Decimal("90")}
        ctx = {"price.AAPL": Decimal("110")}
        with pytest.raises(ValueError, match="unknown cross direction"):
            evaluate(pred, ctx, prev)

    def test_empty_predicate_raises(self) -> None:
        pred: Predicate = {}
        ctx = {"price.AAPL": Decimal("100")}
        with pytest.raises(ValueError, match="invalid predicate"):
            evaluate(pred, ctx)
