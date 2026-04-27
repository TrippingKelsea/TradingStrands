"""Self-Critique execution.

Pure-function core:
    build_context(agent_memory, ledger_snapshot) -> str
    run_self_critique(strands_agent, context) -> SelfCritiqueReport

The pipeline:
    1. Load last 5 trading days of the Strategy Agent's compressed memory.
    2. Load current lessons.md.
    3. Load current ledger snapshot (for realized PnL + current positions).
    4. Build a prompt from the strategy prompt + context.
    5. Invoke an LLM agent to produce a structured reflection.
    6. Append the reflection as a new lesson entry with today's date.

The LLM agent is passed in from the caller — tests provide a stub;
production uses a Strands agent backed by Bedrock. That separation keeps
this module unit-testable without Bedrock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

CRITIQUE_SYSTEM_PROMPT = """\
You are a disciplined trading strategy's weekend reflection coach.

You are reviewing the recorded actions and reasoning of a live trading
strategy over the past week. Your job:

1. Identify patterns in what the strategy actually did. Did it follow its
   own stated rules? Did it make trades that in hindsight were off-thesis?
2. Note decisions whose rationale does not align with the strategy prompt.
3. Highlight missed opportunities where the strategy's rules would have
   suggested an action it did not take.
4. Propose specific, narrow lessons the strategy should remember next week.
   Each lesson must be actionable and specific (not "trade better").

You MAY additionally propose a specific edit to the strategy prompt
itself when a structural problem can't be fixed by a lesson alone
(e.g. a missing rule, an ambiguous entry condition). Prompt edits are
delivered as proposals for the author to review — never auto-applied.

CRITICAL CONSTRAINTS:
- Do NOT invent what the market did. Only reason from the data provided.
  If a claim about market behavior isn't in the context, flag it as
  speculative or omit it.
- Do NOT propose trades the strategy should have made unless you can
  cite the specific observations that would have triggered them.
- Keep the reflection under 500 words. Brevity reflects discipline.
- When proposing a prompt edit, supply the FULL replacement markdown
  (not a diff) — the dashboard renders a diff against the current
  prompt at review time. Explain the rationale separately from the
  replacement text.
"""


@dataclass
class SelfCritiqueReport:
    """Output of one self-critique invocation."""

    bot_id: str
    date: str  # YYYY-MM-DD when this reflection was produced
    reflection: str  # markdown body
    tokens_in: int = 0
    tokens_out: int = 0
    context_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def build_context(
    strategy_prompt: str,
    recent_days: list[tuple[str, str]],
    lessons: str,
    ledger_summary: str,
) -> str:
    """Assemble the prompt context the critique agent reasons over.

    Format is deliberately plain markdown — readable by a human auditor
    in case we ever want to inspect what the critique agent saw."""

    parts: list[str] = [
        "# Strategy prompt (immutable reference)",
        strategy_prompt.strip(),
        "",
        "# Current lessons (prior reflections)",
        lessons.strip() or "_no prior lessons yet_",
        "",
        "# Ledger summary",
        ledger_summary.strip(),
        "",
        "# Recorded memory, newest day first",
    ]
    for date, content in recent_days:
        parts.append(f"\n## {date}")
        parts.append(content.strip() or "_no memory recorded for this day_")
    return "\n".join(parts)


def summarize_ledger(ledger: Any) -> str:
    """One-paragraph summary of ledger state for the critique prompt."""

    if ledger is None:
        return "_ledger unavailable_"
    positions = ", ".join(
        f"{p.symbol} x{p.quantity} @ ${p.burdened_cost_basis}"
        for p in ledger.open_positions
    ) or "none"
    return (
        f"Starting capital: ${ledger.starting_capital}. "
        f"Realized PnL: ${ledger.realized_pnl}. "
        f"High water mark: ${ledger.high_water_mark}. "
        f"Open positions: {positions}."
    )


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def run_self_critique(
    *,
    bot_id: str,
    strategy_prompt: str,
    memory_store: Any,
    ledger: Any,
    llm_invoker: Any,
    recent_days_count: int = 5,
) -> SelfCritiqueReport:
    """Run one self-critique invocation.

    `llm_invoker` is a callable `(system_prompt: str, user_prompt: str) ->
    (response_text: str, tokens_in: int, tokens_out: int)`. In production
    this wraps a Strands Agent; tests pass a stub that returns canned
    text. The wrapper shape is intentionally narrow — self-critique
    doesn't need tool-use, streaming, or structured output.

    On failure, returns a report with the error text in `errors` and an
    empty reflection. The caller decides whether to retry.
    """

    date = _today_utc()
    report = SelfCritiqueReport(bot_id=bot_id, date=date, reflection="")

    try:
        recent = memory_store.load_recent_days(count=recent_days_count)
    except Exception as exc:
        report.errors.append(f"memory.load_recent_days: {exc}")
        return report

    try:
        lessons = memory_store.read_lessons()
    except Exception as exc:
        report.errors.append(f"memory.read_lessons: {exc}")
        lessons = ""

    ledger_summary = summarize_ledger(ledger)
    user_prompt = build_context(
        strategy_prompt=strategy_prompt,
        recent_days=recent,
        lessons=lessons,
        ledger_summary=ledger_summary,
    )
    report.context_bytes = len(user_prompt.encode("utf-8"))

    try:
        response_text, tokens_in, tokens_out = llm_invoker(
            CRITIQUE_SYSTEM_PROMPT, user_prompt,
        )
    except Exception as exc:
        report.errors.append(f"llm_invoker: {exc}")
        return report

    report.tokens_in = tokens_in
    report.tokens_out = tokens_out
    report.reflection = response_text.strip()

    # Append the reflection to lessons.md as a dated entry. The lessons
    # file is append-only across the agent's lifetime (per spec).
    try:
        lesson_entry = (
            f"\n## {date} — weekend self-critique\n\n{report.reflection}\n"
        )
        memory_store.append_lesson(lesson_entry)
    except Exception as exc:
        report.errors.append(f"memory.append_lesson: {exc}")

    logger.info(
        "self_critique.complete bot_id=%s tokens_in=%d tokens_out=%d context_bytes=%d",
        bot_id, tokens_in, tokens_out, report.context_bytes,
    )
    return report


def propose_strategy_edit(
    *,
    proposals_store: Any,
    strategy_id: str,
    org_id: str,
    proposer_agent_id: str,
    rationale: str,
    proposed_markdown: str,
) -> Any:
    """File a strategy-prompt edit proposal from self-critique.

    Callers (the weekend Lambda, or an ad-hoc critique run) use this
    when the reflection surfaces a structural change — a new entry
    rule, a disambiguation of an exit condition, a tightened risk
    clause. The author/orgadmin reviews via the dashboard; proposals
    are never auto-applied.

    Separated from run_self_critique so the decision to file a
    proposal lives with the caller (who has the structured output
    from their LLM), not with this module's prompt-parsing heuristics.
    """

    return proposals_store.create(
        strategy_id=strategy_id,
        org_id=org_id,
        proposer_agent="self_critique",
        proposer_agent_id=proposer_agent_id,
        rationale=rationale,
        proposed_markdown=proposed_markdown,
    )
