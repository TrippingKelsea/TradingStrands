"""Tests for the auditor reconciliation engine (§5.6).

The auditor independently validates ledger-broker consistency and
fee accuracy. These tests cover the deterministic reconciliation
checks — the LLM report generation is tested separately.
"""

from decimal import Decimal

from trading_strands.auditor.reconciler import (
    AuditConfig,
    AuditResult,
    CheckStatus,
    Reconciler,
)
from trading_strands.broker.types import BrokerPosition
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side


def _make_ledger(capital: str = "10000") -> Ledger:
    return Ledger(starting_capital=Decimal(capital))


class TestPositionReconciliation:
    def test_positions_match(self) -> None:
        """Ledger and broker positions agree — should pass."""
        ledger = _make_ledger()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        broker_positions = [
            BrokerPosition(
                symbol="AAPL", quantity=Decimal("10"),
                market_value=Decimal("1500"), current_price=Decimal("150"),
            ),
        ]

        reconciler = Reconciler(AuditConfig())
        result = reconciler.reconcile_positions(
            {"bot-1": ledger}, broker_positions,
        )
        assert result.status == CheckStatus.PASS

    def test_position_quantity_mismatch(self) -> None:
        """Ledger says 10 shares, broker says 15 — should fail."""
        ledger = _make_ledger()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        broker_positions = [
            BrokerPosition(
                symbol="AAPL", quantity=Decimal("15"),
                market_value=Decimal("2250"), current_price=Decimal("150"),
            ),
        ]

        reconciler = Reconciler(AuditConfig())
        result = reconciler.reconcile_positions(
            {"bot-1": ledger}, broker_positions,
        )
        assert result.status == CheckStatus.FAIL
        assert "AAPL" in result.details

    def test_unexpected_broker_position(self) -> None:
        """Broker has a position the ledger doesn't know about."""
        ledger = _make_ledger()
        broker_positions = [
            BrokerPosition(
                symbol="MSFT", quantity=Decimal("20"),
                market_value=Decimal("8000"), current_price=Decimal("400"),
            ),
        ]

        reconciler = Reconciler(AuditConfig())
        result = reconciler.reconcile_positions(
            {"bot-1": ledger}, broker_positions,
        )
        assert result.status == CheckStatus.FAIL
        assert "MSFT" in result.details

    def test_missing_broker_position(self) -> None:
        """Ledger has a position but broker doesn't."""
        ledger = _make_ledger()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))

        reconciler = Reconciler(AuditConfig())
        result = reconciler.reconcile_positions(
            {"bot-1": ledger}, [],
        )
        assert result.status == CheckStatus.FAIL
        assert "AAPL" in result.details

    def test_multi_bot_positions_aggregate(self) -> None:
        """Multiple bots holding same symbol should aggregate for comparison."""
        ledger1 = _make_ledger()
        ledger1.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        ledger2 = _make_ledger()
        ledger2.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("5"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        broker_positions = [
            BrokerPosition(
                symbol="AAPL", quantity=Decimal("15"),
                market_value=Decimal("2250"), current_price=Decimal("150"),
            ),
        ]

        reconciler = Reconciler(AuditConfig())
        result = reconciler.reconcile_positions(
            {"bot-1": ledger1, "bot-2": ledger2}, broker_positions,
        )
        assert result.status == CheckStatus.PASS


class TestFeeReconciliation:
    def test_fees_within_threshold(self) -> None:
        """Fees match expected — should pass."""
        ledger = _make_ledger()
        # Buy first so we can sell
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("100"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.SELL, quantity=Decimal("100"),
            price=Decimal("150.00"),
            fees=FeeBreakdown(sec_fee=Decimal("1.20"), taf_fee=Decimal("0.02")),
        ))

        # Auditor's independent fee schedule: buy has no fees, sell matches
        auditor_fees_buy = FeeBreakdown()
        auditor_fees_sell = FeeBreakdown(sec_fee=Decimal("1.20"), taf_fee=Decimal("0.02"))

        reconciler = Reconciler(AuditConfig(fee_drift_threshold_pct=Decimal("0.05")))
        result = reconciler.reconcile_fees(
            {"bot-1": ledger}, [auditor_fees_buy, auditor_fees_sell],
        )
        assert result.status == CheckStatus.PASS

    def test_fees_exceed_threshold(self) -> None:
        """Fees diverge beyond threshold — should fail."""
        ledger = _make_ledger()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("100"),
            price=Decimal("150.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.SELL, quantity=Decimal("100"),
            price=Decimal("150.00"),
            fees=FeeBreakdown(sec_fee=Decimal("1.20"), taf_fee=Decimal("0.02")),
        ))

        # Auditor expects significantly different fees
        auditor_fees_buy = FeeBreakdown()
        auditor_fees_sell = FeeBreakdown(sec_fee=Decimal("2.50"), taf_fee=Decimal("0.10"))

        reconciler = Reconciler(AuditConfig(fee_drift_threshold_pct=Decimal("0.05")))
        result = reconciler.reconcile_fees(
            {"bot-1": ledger}, [auditor_fees_buy, auditor_fees_sell],
        )
        assert result.status == CheckStatus.FAIL


class TestKillSwitchThresholds:
    def test_drift_below_threshold_no_kill(self) -> None:
        config = AuditConfig(
            fee_drift_threshold_abs=Decimal("10.00"),
            position_mismatch_cycles=3,
        )
        reconciler = Reconciler(config)
        assert not reconciler.should_kill_switch(
            fee_drift=Decimal("5.00"),
            mismatch_cycles=1,
        )

    def test_fee_drift_triggers_kill(self) -> None:
        config = AuditConfig(fee_drift_threshold_abs=Decimal("10.00"))
        reconciler = Reconciler(config)
        assert reconciler.should_kill_switch(
            fee_drift=Decimal("15.00"),
            mismatch_cycles=0,
        )

    def test_persistent_mismatch_triggers_kill(self) -> None:
        config = AuditConfig(position_mismatch_cycles=3)
        reconciler = Reconciler(config)
        assert reconciler.should_kill_switch(
            fee_drift=Decimal("0"),
            mismatch_cycles=3,
        )


class TestAuditResult:
    def test_all_pass(self) -> None:
        result = AuditResult(
            position_check=AuditResult.Check(status=CheckStatus.PASS, details=""),
            fee_check=AuditResult.Check(status=CheckStatus.PASS, details=""),
            kill_switch_triggered=False,
        )
        assert result.all_passed

    def test_any_fail(self) -> None:
        result = AuditResult(
            position_check=AuditResult.Check(
                status=CheckStatus.FAIL, details="AAPL mismatch",
            ),
            fee_check=AuditResult.Check(status=CheckStatus.PASS, details=""),
            kill_switch_triggered=False,
        )
        assert not result.all_passed
