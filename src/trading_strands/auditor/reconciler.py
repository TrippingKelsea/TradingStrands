"""Auditor reconciliation engine (§5.6).

Deterministic checks for ledger-broker consistency and fee accuracy.
The LLM-generated audit report wraps these results with natural-language
analysis — this module contains only the deterministic logic.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from trading_strands.broker.types import BrokerPosition
from trading_strands.ledger.models import FeeBreakdown, Ledger


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"


class AuditConfig(BaseModel):
    """User-configurable drift thresholds for the auditor (§5.6.3)."""

    fee_drift_threshold_pct: Decimal = Decimal("0.05")  # 5%
    fee_drift_threshold_abs: Decimal = Decimal("10.00")  # $10
    position_mismatch_cycles: int = 3  # consecutive cycles before kill switch


class AuditResult(BaseModel):
    """Structured audit report (§5.6.4)."""

    class Check(BaseModel):
        status: CheckStatus
        details: str = ""

    position_check: Check
    fee_check: Check
    kill_switch_triggered: bool = False

    @property
    def all_passed(self) -> bool:
        return (
            self.position_check.status == CheckStatus.PASS
            and self.fee_check.status == CheckStatus.PASS
        )


class Reconciler:
    """Deterministic reconciliation checks."""

    def __init__(self, config: AuditConfig) -> None:
        self._config = config

    def reconcile_positions(
        self,
        ledgers: dict[str, Ledger],
        broker_positions: list[BrokerPosition],
    ) -> AuditResult.Check:
        """Compare aggregate ledger positions against broker positions (§5.6.1)."""
        # Aggregate ledger positions across all bots
        ledger_totals: dict[str, Decimal] = {}
        for ledger in ledgers.values():
            for pos in ledger.open_positions:
                ledger_totals[pos.symbol] = (
                    ledger_totals.get(pos.symbol, Decimal("0")) + pos.quantity
                )

        # Build broker totals
        broker_totals: dict[str, Decimal] = {}
        for bpos in broker_positions:
            broker_totals[bpos.symbol] = (
                broker_totals.get(bpos.symbol, Decimal("0")) + bpos.quantity
            )

        # Compare
        all_symbols = set(ledger_totals.keys()) | set(broker_totals.keys())
        mismatches: list[str] = []

        for symbol in sorted(all_symbols):
            ledger_qty = ledger_totals.get(symbol, Decimal("0"))
            broker_qty = broker_totals.get(symbol, Decimal("0"))
            if ledger_qty != broker_qty:
                mismatches.append(
                    f"{symbol}: ledger={ledger_qty}, broker={broker_qty}"
                )

        if mismatches:
            return AuditResult.Check(
                status=CheckStatus.FAIL,
                details="; ".join(mismatches),
            )
        return AuditResult.Check(status=CheckStatus.PASS)

    def reconcile_fees(
        self,
        ledgers: dict[str, Ledger],
        expected_fees: list[FeeBreakdown],
    ) -> AuditResult.Check:
        """Compare recorded fees against auditor's independent calculation (§5.6.2).

        The auditor maintains its own fee schedule. This method compares
        the total fees recorded in the ledger against the auditor's
        expected fees for the same trades.
        """
        # Sum all recorded fees across all bots
        recorded_total = Decimal("0")
        for ledger in ledgers.values():
            for fee in ledger.fee_ledger:
                recorded_total += fee.total

        # Sum auditor's expected fees
        expected_total = sum(
            (fee.total for fee in expected_fees), Decimal("0"),
        )

        if expected_total == 0 and recorded_total == 0:
            return AuditResult.Check(status=CheckStatus.PASS)

        drift = abs(recorded_total - expected_total)
        base = max(recorded_total, expected_total)
        drift_pct = drift / base if base > 0 else Decimal("0")

        if drift_pct > self._config.fee_drift_threshold_pct:
            return AuditResult.Check(
                status=CheckStatus.FAIL,
                details=(
                    f"fee drift: recorded={recorded_total}, "
                    f"expected={expected_total}, "
                    f"drift={drift_pct:.2%}"
                ),
            )
        return AuditResult.Check(status=CheckStatus.PASS)

    def should_kill_switch(
        self,
        fee_drift: Decimal,
        mismatch_cycles: int,
    ) -> bool:
        """Determine if drift thresholds warrant a kill switch (§5.6.3)."""
        if fee_drift > self._config.fee_drift_threshold_abs:
            return True
        return mismatch_cycles >= self._config.position_mismatch_cycles
