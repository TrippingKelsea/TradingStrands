"""Tests for the Self-Critique runner.

Uses an in-memory stub for the memory store and a canned-response
LLM invoker so the tests exercise the whole pipeline without touching
S3 or Bedrock.
"""

from __future__ import annotations

from decimal import Decimal

from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.self_critique.runner import (
    CRITIQUE_SYSTEM_PROMPT,
    SelfCritiqueReport,
    build_context,
    run_self_critique,
    summarize_ledger,
)


class FakeMemoryStore:
    """In-memory stub of AgentMemoryStore — matches the methods the
    self-critique runner uses (load_recent_days, read_lessons, append_lesson)."""

    def __init__(
        self, days: list[tuple[str, str]] | None = None,
        lessons: str = "",
    ) -> None:
        self._days = days or []
        self._lessons = lessons
        self.appended_lessons: list[str] = []

    def load_recent_days(
        self, count: int, end_date: str | None = None,
    ) -> list[tuple[str, str]]:
        return list(self._days[:count])

    def read_lessons(self) -> str:
        return self._lessons

    def append_lesson(self, text: str) -> None:
        self.appended_lessons.append(text)
        self._lessons += ("\n" if self._lessons else "") + text


def _stub_invoker(canned_text: str, tokens_in: int = 200, tokens_out: int = 80):
    def _invoke(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        assert system_prompt == CRITIQUE_SYSTEM_PROMPT
        # The caller passes the full context; keep it around for assertions
        # via attribute access on the inner function itself.
        _invoke.last_user_prompt = user_prompt  # type: ignore[attr-defined]
        return canned_text, tokens_in, tokens_out
    return _invoke


def _ledger_with_pnl() -> Ledger:
    ledger = Ledger(starting_capital=Decimal("10000"))
    buy = Fill(
        symbol="SPY", side=Side.BUY,
        quantity=Decimal("10"), price=Decimal("500"),
        fees=FeeBreakdown(commission=Decimal("1")),
    )
    sell = Fill(
        symbol="SPY", side=Side.SELL,
        quantity=Decimal("10"), price=Decimal("510"),
        fees=FeeBreakdown(commission=Decimal("1")),
    )
    ledger.record_fill(buy)
    ledger.record_fill(sell)
    return ledger


# ── build_context ────────────────────────────────────────────────────


def test_build_context_includes_all_sections() -> None:
    days = [
        ("2026-04-26", "## Actions\n- bought SPY"),
        ("2026-04-25", "## Actions\n- held"),
    ]
    ctx = build_context(
        strategy_prompt="Buy SPY on RSI<30.",
        recent_days=days,
        lessons="Past lesson about FOMC.",
        ledger_summary="Summary text.",
    )
    assert "Strategy prompt" in ctx
    assert "Buy SPY on RSI<30." in ctx
    assert "Current lessons" in ctx
    assert "Past lesson about FOMC." in ctx
    assert "Ledger summary" in ctx
    assert "Summary text." in ctx
    assert "2026-04-26" in ctx
    assert "2026-04-25" in ctx
    # Newest day appears first in the memory section.
    assert ctx.index("2026-04-26") < ctx.index("2026-04-25")


def test_build_context_handles_empty_lessons_and_missing_days() -> None:
    days = [
        ("2026-04-26", ""),   # no memory for this day (gap)
        ("2026-04-25", "recorded"),
    ]
    ctx = build_context(
        strategy_prompt="strat",
        recent_days=days,
        lessons="",
        ledger_summary="_ledger_",
    )
    assert "no prior lessons yet" in ctx
    assert "no memory recorded for this day" in ctx
    assert "recorded" in ctx


# ── summarize_ledger ─────────────────────────────────────────────────


def test_summarize_ledger_with_positions() -> None:
    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(Fill(
        symbol="AAPL", side=Side.BUY,
        quantity=Decimal("5"), price=Decimal("150"),
        fees=FeeBreakdown(commission=Decimal("1")),
    ))
    summary = summarize_ledger(ledger)
    assert "10000" in summary
    assert "AAPL" in summary
    assert "x5" in summary


def test_summarize_ledger_no_open_positions() -> None:
    ledger = _ledger_with_pnl()  # opens and closes SPY; no open positions
    summary = summarize_ledger(ledger)
    assert "none" in summary
    # Realized PnL should appear (non-zero — bought 500, sold 510).
    assert "Realized PnL" in summary


def test_summarize_ledger_handles_none() -> None:
    assert "unavailable" in summarize_ledger(None)


# ── run_self_critique ────────────────────────────────────────────────


def test_full_run_appends_lesson() -> None:
    mem = FakeMemoryStore(days=[
        ("2026-04-26", "## Actions\n- bought SPY @ 500 on RSI signal"),
        ("2026-04-25", ""),
    ])
    ledger = _ledger_with_pnl()
    invoker = _stub_invoker(
        "## Observations\n\nStrategy followed its RSI rule on 2026-04-26.",
    )

    report = run_self_critique(
        bot_id="bot-1",
        strategy_prompt="Buy SPY on RSI<30.",
        memory_store=mem,
        ledger=ledger,
        llm_invoker=invoker,
    )

    assert isinstance(report, SelfCritiqueReport)
    assert report.bot_id == "bot-1"
    assert report.reflection.startswith("## Observations")
    assert report.tokens_in == 200
    assert report.tokens_out == 80
    assert report.context_bytes > 0
    assert report.errors == []

    # Lesson was appended and contains today's dated header.
    assert len(mem.appended_lessons) == 1
    appended = mem.appended_lessons[0]
    assert "weekend self-critique" in appended
    # Today's date appears in the header.
    from trading_strands.self_critique.runner import _today_utc
    assert _today_utc() in appended


def test_llm_failure_produces_error_report_no_lesson_appended() -> None:
    """If the LLM raises, no lesson is written — we don't want to log
    an empty reflection that readers might mistake for 'nothing to learn'."""

    mem = FakeMemoryStore(days=[("2026-04-26", "data")])
    ledger = _ledger_with_pnl()

    def broken_invoker(system: str, user: str) -> tuple[str, int, int]:
        raise RuntimeError("bedrock timeout")

    report = run_self_critique(
        bot_id="bot-1",
        strategy_prompt="strat",
        memory_store=mem,
        ledger=ledger,
        llm_invoker=broken_invoker,
    )
    assert report.reflection == ""
    assert any("bedrock timeout" in e for e in report.errors)
    assert mem.appended_lessons == []


def test_memory_load_failure_aborts_cleanly() -> None:
    """If we can't even read the memory, return an error report without
    invoking the LLM (no tokens burned on a failed run)."""

    class BrokenMemory:
        def load_recent_days(
            self, count: int, end_date: str | None = None,
        ) -> list[tuple[str, str]]:
            raise RuntimeError("s3 unavailable")

        def read_lessons(self) -> str:
            raise AssertionError("shouldn't be called")

        def append_lesson(self, text: str) -> None:
            raise AssertionError("shouldn't be called")

    invoker = _stub_invoker("unused")
    report = run_self_critique(
        bot_id="bot-1",
        strategy_prompt="s",
        memory_store=BrokenMemory(),
        ledger=Ledger(starting_capital=Decimal("1000")),
        llm_invoker=invoker,
    )
    assert report.reflection == ""
    assert any("s3 unavailable" in e for e in report.errors)


def test_recent_days_count_is_respected() -> None:
    """Caller can control how many days to review (default 5)."""

    days = [(f"2026-04-{26-i:02d}", f"day {i}") for i in range(10)]
    mem = FakeMemoryStore(days=days)
    invoker = _stub_invoker("ok")

    run_self_critique(
        bot_id="bot-1",
        strategy_prompt="s",
        memory_store=mem,
        ledger=_ledger_with_pnl(),
        llm_invoker=invoker,
        recent_days_count=3,
    )

    last_prompt = invoker.last_user_prompt  # type: ignore[attr-defined]
    # Should contain the 3 most-recent dates but not the 4th.
    assert "2026-04-26" in last_prompt
    assert "2026-04-25" in last_prompt
    assert "2026-04-24" in last_prompt
    assert "2026-04-23" not in last_prompt


def test_lambda_load_strategy_prompt_from_ddb() -> None:
    """The Lambda handler's helper that reads a strategy prompt from DDB."""

    import boto3
    from moto import mock_aws

    from trading_strands.self_critique.lambda_handler import _load_strategy_prompt

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName="t",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("t")
        table.put_item(Item={
            "pk": "STRATEGY#abc",
            "strategy_id": "abc",
            "markdown": "## Rules\n- buy on dips",
        })
        # bot_id is 'strategy-abc' per app.py convention.
        assert "buy on dips" in _load_strategy_prompt(table, "strategy-abc")
        # Missing strategy returns empty string, not error.
        assert _load_strategy_prompt(table, "strategy-missing") == ""


def test_critique_system_prompt_forbids_phantom_trades() -> None:
    """Hard invariant: the system prompt must tell the LLM not to invent
    market behavior or propose simulated trades — that's backtesting,
    which CLAUDE.md forbids."""

    assert "Do NOT invent" in CRITIQUE_SYSTEM_PROMPT
    assert "cite" in CRITIQUE_SYSTEM_PROMPT or "citing" in CRITIQUE_SYSTEM_PROMPT.lower()
    assert "strategy prompt itself" in CRITIQUE_SYSTEM_PROMPT
