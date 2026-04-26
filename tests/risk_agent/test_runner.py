"""Tests for the Risk Agent runner.

Pattern mirrors Self-Critique: in-memory memory store stub, canned-
response LLM invoker, assert the context assembly and the output
appending. Production wiring is tested separately at the Lambda
handler level.
"""

from __future__ import annotations

from decimal import Decimal

from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.risk_agent.runner import (
    RISK_SYSTEM_PROMPT,
    RiskReviewReport,
    build_context,
    run_risk_review,
    summarize_fleet,
)


class FakeMemoryStore:
    """In-memory stub of AgentMemoryStore — the Risk Agent only needs
    read_recommendations and append_recommendation."""

    def __init__(self, recommendations: str = "") -> None:
        self._recs = recommendations
        self.appended: list[str] = []

    def read_recommendations(self) -> str:
        return self._recs

    def append_recommendation(self, text: str) -> None:
        self.appended.append(text)
        self._recs += ("\n" if self._recs else "") + text


def _stub_invoker(text: str, tokens_in: int = 100, tokens_out: int = 60):
    def _invoke(system: str, user: str) -> tuple[str, int, int]:
        assert system == RISK_SYSTEM_PROMPT
        _invoke.last_user_prompt = user  # type: ignore[attr-defined]
        return text, tokens_in, tokens_out
    return _invoke


def _ledger_with_concentration() -> Ledger:
    """Ledger with one dominant position — should trigger concentration
    reasoning in the prompt (whether the LLM calls it out is up to the
    LLM; the context must carry the signal)."""

    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(Fill(
        symbol="NVDA", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("800"),
        fees=FeeBreakdown(commission=Decimal("1")),
    ))
    return ledger


# ── summarize_fleet ──────────────────────────────────────────────────


def test_summarize_fleet_rolls_up_bots() -> None:
    """The Risk Agent reviews the whole org — multiple bots, one report."""

    a = Ledger(starting_capital=Decimal("5000"))
    a.record_fill(Fill(
        symbol="AAPL", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("150"),
        fees=FeeBreakdown(commission=Decimal("1")),
    ))
    b = _ledger_with_concentration()

    summary = summarize_fleet({"strategy-1": a, "strategy-2": b})
    assert "strategy-1" in summary
    assert "strategy-2" in summary
    assert "AAPL" in summary
    assert "NVDA" in summary
    # Total starting capital across bots surfaces as a single number.
    assert "15000" in summary


def test_summarize_fleet_empty_returns_placeholder() -> None:
    assert "no bots" in summarize_fleet({})


# ── build_context ────────────────────────────────────────────────────


def test_build_context_has_all_sections() -> None:
    ctx = build_context(
        org_id="org-a",
        fleet_summary="Summary line.",
        prior_recommendations="Past note about drawdown.",
        recent_fills=[
            {"ts": 1700000000, "fill_json": '{"symbol":"NVDA"}'},
        ],
    )
    assert "org-a" in ctx
    assert "Summary line." in ctx
    assert "Past note about drawdown." in ctx
    assert "NVDA" in ctx


def test_build_context_empty_prior_and_fills() -> None:
    ctx = build_context(
        org_id="org-a",
        fleet_summary="Summary",
        prior_recommendations="",
        recent_fills=[],
    )
    assert "no prior recommendations" in ctx
    assert "no recent fills" in ctx


# ── run_risk_review ──────────────────────────────────────────────────


def test_full_run_appends_recommendation() -> None:
    mem = FakeMemoryStore()
    invoker = _stub_invoker(
        "Concentration in NVDA exceeds 40% of equity. "
        "Consider trimming to 25%.",
    )
    report = run_risk_review(
        org_id="org-a",
        memory_store=mem,
        ledgers={"strategy-1": _ledger_with_concentration()},
        recent_fills=[],
        llm_invoker=invoker,
    )

    assert isinstance(report, RiskReviewReport)
    assert report.org_id == "org-a"
    assert "NVDA" in report.recommendation
    assert report.tokens_in == 100
    assert report.tokens_out == 60
    assert report.errors == []

    # Appended once with today's date header.
    assert len(mem.appended) == 1
    appended = mem.appended[0]
    assert "risk review" in appended


def test_llm_failure_produces_error_report_no_append() -> None:
    """If the LLM raises, we don't silently record an empty review —
    orgadmins would mistake blank entries for 'nothing to flag'."""

    mem = FakeMemoryStore()

    def broken(system: str, user: str) -> tuple[str, int, int]:
        raise RuntimeError("bedrock throttled")

    report = run_risk_review(
        org_id="org-a",
        memory_store=mem,
        ledgers={"s1": _ledger_with_concentration()},
        recent_fills=[],
        llm_invoker=broken,
    )
    assert report.recommendation == ""
    assert any("bedrock throttled" in e for e in report.errors)
    assert mem.appended == []


def test_read_recommendations_failure_falls_back_to_empty() -> None:
    """If prior-recommendations can't be read (e.g. S3 throttled), we
    still produce a review — we just don't include prior context in
    the prompt. Missing prior history is strictly less-bad than
    skipping the whole review."""

    class FlakyMem:
        def read_recommendations(self) -> str:
            raise RuntimeError("s3 throttled")

        def append_recommendation(self, text: str) -> None:
            self.last = text

    mem = FlakyMem()
    invoker = _stub_invoker("ok")
    report = run_risk_review(
        org_id="org-a",
        memory_store=mem,
        ledgers={"s1": Ledger(starting_capital=Decimal("100"))},
        recent_fills=[],
        llm_invoker=invoker,
    )
    # Recorded the read error but still produced a recommendation.
    assert any("read_recommendations" in e for e in report.errors)
    assert report.recommendation == "ok"


def test_system_prompt_pins_its_role() -> None:
    """Invariant: the risk system prompt must tell the LLM not to
    propose trades or attempt to change the deterministic risk config —
    it's a recommender, not an actor."""

    assert "do not propose trades" in RISK_SYSTEM_PROMPT.lower() or (
        "not to propose trades" in RISK_SYSTEM_PROMPT.lower()
    )
    assert "deterministic" in RISK_SYSTEM_PROMPT.lower()
    assert "recommend" in RISK_SYSTEM_PROMPT.lower()
