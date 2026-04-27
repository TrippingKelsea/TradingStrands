"""Tests for the Auditor Agent runner.

Auditor is the agentified v0 Reconciler. It:
  1. Runs the deterministic position-reconciliation check.
  2. Hands the result to an LLM for human-consumable interpretation.
  3. Appends to recommendations.md.
  4. If the check failed, sets a desk-halt via the CONTROL row.

Tests keep the pieces separately:
  - classification + halt decision (deterministic, pure function)
  - LLM interpretation (stub invoker)
  - halt-writer side effect (records-calls fake)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_strands.auditor.reconciler import AuditResult, CheckStatus
from trading_strands.auditor_agent.runner import (
    AUDITOR_SYSTEM_PROMPT,
    AuditReviewReport,
    build_context,
    decide_halt,
    run_audit_review,
)
from trading_strands.broker.types import BrokerPosition
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side


class FakeMemoryStore:
    def __init__(self) -> None:
        self._recs = ""
        self.appended: list[str] = []

    def read_recommendations(self) -> str:
        return self._recs

    def append_recommendation(self, text: str) -> None:
        self.appended.append(text)
        self._recs += ("\n" if self._recs else "") + text


class FakeHaltControl:
    """Records halt/unhalt calls instead of writing to DDB."""

    def __init__(self) -> None:
        self.halted: bool | None = None
        self.reason: str | None = None

    def set_halted(self, halted: bool, reason: str = "") -> None:
        self.halted = halted
        self.reason = reason


def _stub_invoker(text: str) -> Any:
    def _invoke(system: str, user: str) -> tuple[str, int, int]:
        assert system == AUDITOR_SYSTEM_PROMPT
        _invoke.last_user = user  # type: ignore[attr-defined]
        return text, 80, 40
    return _invoke


def _ledgers_with_positions(
    symbols: dict[str, Decimal],
) -> dict[str, Ledger]:
    """Build one bot's ledger holding the given {symbol: qty}."""

    ledger = Ledger(starting_capital=Decimal("10000"))
    for sym, qty in symbols.items():
        ledger.record_fill(Fill(
            symbol=sym, side=Side.BUY,
            quantity=qty, price=Decimal("100"),
            fees=FeeBreakdown(commission=Decimal("1")),
        ))
    return {"strategy-1": ledger}


# ── decide_halt ──────────────────────────────────────────────────────


def test_decide_halt_pass_means_no_halt() -> None:
    """Clean reconciliation = never halt."""

    check = AuditResult.Check(status=CheckStatus.PASS)
    assert decide_halt(check) is False


def test_decide_halt_fail_means_halt() -> None:
    """Any position mismatch halts. We do NOT require multi-cycle
    confirmation at the agent layer — that's the v0 Reconciler's
    consecutive-cycle logic; the Auditor Agent runs periodically and
    a single observed drift is itself noteworthy."""

    check = AuditResult.Check(
        status=CheckStatus.FAIL,
        details="AAPL: ledger=10, broker=8",
    )
    assert decide_halt(check) is True


# ── build_context ────────────────────────────────────────────────────


def test_build_context_shows_check_and_positions() -> None:
    check = AuditResult.Check(
        status=CheckStatus.FAIL,
        details="AAPL: ledger=10, broker=8",
    )
    ctx = build_context(
        org_id="org-a",
        check=check,
        ledger_totals={"AAPL": Decimal("10")},
        broker_totals={"AAPL": Decimal("8")},
        prior_recommendations="",
    )
    assert "org-a" in ctx
    assert "FAIL" in ctx
    assert "AAPL" in ctx
    # The raw totals must show up so the LLM can reason about the
    # direction of the drift (did we over-fill or did the broker
    # under-execute).
    assert "ledger=10" in ctx
    assert "broker=8" in ctx


def test_build_context_pass_case_is_concise() -> None:
    """A passing reconciliation still runs the LLM (to write a 'desk
    healthy' entry for the week), but the context is minimal."""

    check = AuditResult.Check(status=CheckStatus.PASS)
    ctx = build_context(
        org_id="org-a",
        check=check,
        ledger_totals={},
        broker_totals={},
        prior_recommendations="",
    )
    assert "PASS" in ctx


# ── run_audit_review ─────────────────────────────────────────────────


def test_run_audit_review_pass_no_halt() -> None:
    mem = FakeMemoryStore()
    halt = FakeHaltControl()
    invoker = _stub_invoker("Desk is reconciled. No drift this cycle.")

    ledgers = _ledgers_with_positions({"AAPL": Decimal("10")})
    broker_positions = [
        BrokerPosition(
            symbol="AAPL", quantity=Decimal("10"),
            market_value=Decimal("1000"), current_price=Decimal("100"),
        ),
    ]

    report = run_audit_review(
        org_id="org-a",
        memory_store=mem,
        halt_control=halt,
        ledgers=ledgers,
        broker_positions=broker_positions,
        llm_invoker=invoker,
    )

    assert isinstance(report, AuditReviewReport)
    assert report.check_status == "pass"
    assert report.halt_triggered is False
    assert halt.halted is None  # never called
    assert len(mem.appended) == 1


def test_run_audit_review_fail_triggers_halt() -> None:
    """Broker shows 8 shares, ledger says 10: drift. Must halt."""

    mem = FakeMemoryStore()
    halt = FakeHaltControl()
    invoker = _stub_invoker("Position drift on AAPL; halting desk.")

    ledgers = _ledgers_with_positions({"AAPL": Decimal("10")})
    broker_positions = [
        BrokerPosition(
            symbol="AAPL", quantity=Decimal("8"),
            market_value=Decimal("800"), current_price=Decimal("100"),
        ),
    ]

    report = run_audit_review(
        org_id="org-a",
        memory_store=mem,
        halt_control=halt,
        ledgers=ledgers,
        broker_positions=broker_positions,
        llm_invoker=invoker,
    )

    assert report.check_status == "fail"
    assert report.halt_triggered is True
    assert halt.halted is True
    assert halt.reason is not None
    # The halt reason should reference the auditor so operators in
    # the dashboard can tell the source.
    assert "auditor" in (halt.reason or "").lower()


def test_run_audit_review_halt_writer_failure_does_not_block_recommendation() -> None:
    """If the halt write itself fails (DDB throttle, auth, etc.), the
    Auditor must still append its recommendation — the operator
    needs to see the drift even if automation didn't land the halt."""

    mem = FakeMemoryStore()

    class BrokenHalt:
        def set_halted(self, halted: bool, reason: str = "") -> None:
            raise RuntimeError("ddb throttle")

    halt = BrokenHalt()
    invoker = _stub_invoker("Drift detected.")

    ledgers = _ledgers_with_positions({"AAPL": Decimal("10")})
    broker_positions = [
        BrokerPosition(
            symbol="AAPL", quantity=Decimal("8"),
            market_value=Decimal("800"), current_price=Decimal("100"),
        ),
    ]
    report = run_audit_review(
        org_id="org-a",
        memory_store=mem,
        halt_control=halt,
        ledgers=ledgers,
        broker_positions=broker_positions,
        llm_invoker=invoker,
    )
    assert report.halt_triggered is True  # we *intended* to halt
    assert any("halt_control" in e for e in report.errors)
    assert len(mem.appended) == 1  # recommendation still recorded


def test_llm_failure_does_not_block_halt() -> None:
    """If the LLM fails on a FAIL check, we still halt. The whole
    point of the deterministic check is that it's authoritative;
    the LLM just writes the narrative."""

    mem = FakeMemoryStore()
    halt = FakeHaltControl()

    def broken(s: str, u: str) -> tuple[str, int, int]:
        raise RuntimeError("bedrock down")

    ledgers = _ledgers_with_positions({"AAPL": Decimal("10")})
    broker_positions = [
        BrokerPosition(
            symbol="AAPL", quantity=Decimal("8"),
            market_value=Decimal("800"), current_price=Decimal("100"),
        ),
    ]
    report = run_audit_review(
        org_id="org-a",
        memory_store=mem,
        halt_control=halt,
        ledgers=ledgers,
        broker_positions=broker_positions,
        llm_invoker=broken,
    )
    # LLM failed, recommendation empty, but halt still fired.
    assert report.recommendation == ""
    assert halt.halted is True
    assert report.halt_triggered is True
    assert any("llm_invoker" in e for e in report.errors)


def test_system_prompt_pins_halt_authority() -> None:
    """The auditor has halt authority. The prompt must make clear
    that halting is a real action, not advisory — so the LLM
    doesn't soften language when drift is real."""

    lower = AUDITOR_SYSTEM_PROMPT.lower()
    assert "halt" in lower
    assert "reconcil" in lower  # reconcile/reconciliation
    # Must also forbid proposing trades; the auditor is a reviewer,
    # not an actor.
    assert "do not propose trades" in lower or (
        "not to propose trades" in lower
    )
