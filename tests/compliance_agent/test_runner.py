"""Tests for the Compliance Agent runner."""

from __future__ import annotations

from decimal import Decimal

from trading_strands.compliance_agent.runner import (
    COMPLIANCE_SYSTEM_PROMPT,
    ComplianceReviewReport,
    StrategyMandate,
    build_context,
    run_compliance_review,
)
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side


class FakeMemoryStore:
    def __init__(self, recommendations: str = "") -> None:
        self._recs = recommendations
        self.appended: list[str] = []

    def read_recommendations(self) -> str:
        return self._recs

    def append_recommendation(self, text: str) -> None:
        self.appended.append(text)
        self._recs += ("\n" if self._recs else "") + text


def _stub_invoker(text: str, tin: int = 150, tout: int = 90):
    def _invoke(system: str, user: str) -> tuple[str, int, int]:
        assert system == COMPLIANCE_SYSTEM_PROMPT
        _invoke.last_user = user  # type: ignore[attr-defined]
        return text, tin, tout
    return _invoke


def _mandate(
    name: str = "Long Tech Value", markdown: str = "Buy high-quality tech on dips.",
    symbols: list[str] | None = None,
) -> StrategyMandate:
    """One strategy's declared mandate + observed activity context."""

    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(Fill(
        symbol="AAPL", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("150"),
        fees=FeeBreakdown(commission=Decimal("1")),
    ))
    return StrategyMandate(
        strategy_id="abc",
        bot_id="strategy-abc",
        name=name,
        prompt_markdown=markdown,
        declared_symbols=symbols or ["AAPL", "MSFT"],
        ledger=ledger,
        recent_fills=[
            {"ts": 1700000000, "fill_json": '{"symbol":"AAPL","side":"buy"}'},
        ],
    )


# ── build_context ────────────────────────────────────────────────────


def test_build_context_includes_all_strategies() -> None:
    ctx = build_context(
        org_id="org-a",
        strategies=[
            _mandate(name="Long Tech Value"),
            _mandate(name="Short Utilities"),
        ],
        prior_recommendations="",
    )
    assert "org-a" in ctx
    assert "Long Tech Value" in ctx
    assert "Short Utilities" in ctx
    # Each strategy's declared mandate (prompt) must appear verbatim —
    # the agent needs the original text to judge drift.
    assert ctx.count("Buy high-quality tech") == 2


def test_build_context_empty_prior() -> None:
    ctx = build_context(
        org_id="org-a", strategies=[], prior_recommendations="",
    )
    assert "no strategies" in ctx


def test_build_context_handles_prior_recommendations() -> None:
    ctx = build_context(
        org_id="org-a", strategies=[_mandate()],
        prior_recommendations="## 2026-04-19\n\nPrior concern about X.",
    )
    assert "Prior concern about X" in ctx


# ── run_compliance_review ───────────────────────────────────────────


def test_full_run_appends_recommendation() -> None:
    mem = FakeMemoryStore()
    invoker = _stub_invoker(
        "Strategy 'Long Tech Value' appears consistent with mandate.",
    )
    report = run_compliance_review(
        org_id="org-a",
        memory_store=mem,
        strategies=[_mandate()],
        llm_invoker=invoker,
    )

    assert isinstance(report, ComplianceReviewReport)
    assert report.org_id == "org-a"
    assert "consistent with mandate" in report.recommendation
    assert report.errors == []
    assert len(mem.appended) == 1
    assert "compliance review" in mem.appended[0]


def test_llm_failure_produces_error_no_append() -> None:
    mem = FakeMemoryStore()

    def broken(s: str, u: str) -> tuple[str, int, int]:
        raise RuntimeError("bedrock 429")

    report = run_compliance_review(
        org_id="org-a",
        memory_store=mem,
        strategies=[_mandate()],
        llm_invoker=broken,
    )
    assert report.recommendation == ""
    assert any("bedrock 429" in e for e in report.errors)
    assert mem.appended == []


def test_empty_strategies_produces_no_op_report() -> None:
    """No strategies = nothing to audit. Return a report marked as
    skipped without calling the LLM — we don't want to bill tokens
    for asking an empty question."""

    mem = FakeMemoryStore()

    class AssertingInvoker:
        def __call__(self, s: str, u: str) -> tuple[str, int, int]:
            raise AssertionError("should not invoke LLM with no strategies")

    report = run_compliance_review(
        org_id="org-a",
        memory_store=mem,
        strategies=[],
        llm_invoker=AssertingInvoker(),
    )
    assert report.recommendation == ""
    assert report.skipped_reason == "no strategies"
    assert mem.appended == []


def test_system_prompt_forbids_changing_strategy() -> None:
    """Compliance reviews strategies, doesn't author them."""

    assert "drift" in COMPLIANCE_SYSTEM_PROMPT.lower()
    assert "mandate" in COMPLIANCE_SYSTEM_PROMPT.lower()
    # Invariant: must tell the LLM to recommend, not to edit.
    assert "orgadmin" in COMPLIANCE_SYSTEM_PROMPT.lower()
