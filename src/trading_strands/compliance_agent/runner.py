"""Compliance Agent runner.

Reviews strategy drift — does each strategy's recent behavior match
the mandate declared in its prompt? Also a natural home for regulatory
posture (PDT, wash-sale pattern, cross-org leakage) as those rules
land.

Invocation is per-org; the runner iterates strategies internally so a
single prompt can reason across the fleet and surface patterns no
single-strategy view would notice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

COMPLIANCE_SYSTEM_PROMPT = """\
You are a compliance reviewer for an automated trading organization.
You read each active strategy's declared mandate (its prompt) and its
observed recent activity, and produce short, specific recommendations
for a human orgadmin.

Your scope:
- Strategy drift: is the strategy's recent behavior consistent with
  its declared mandate? Flag when a "long-only value" strategy takes
  shorts, or when a strategy described for one sector trades another.
- Regulatory posture: concerning patterns around PDT, wash sales, or
  inconsistent sizing relative to stated risk rules.
- Cross-strategy concerns within the org: two strategies that claim
  different mandates but actually take the same trades for the same
  reasons.

CRITICAL CONSTRAINTS:
- Do NOT propose edits to strategy prompts. Recommend that the
  orgadmin review and decide. Author-of-record rules apply.
- Do NOT propose trades. You review; you do not act.
- Do NOT invent facts not in the context.
- Keep each recommendation under 300 words. Specific, actionable,
  framed as observation + concern + what the orgadmin might consider.
"""


@dataclass
class StrategyMandate:
    """One strategy's mandate + observed activity context, ready for
    the reviewer to consume."""

    strategy_id: str
    bot_id: str
    name: str
    prompt_markdown: str
    declared_symbols: list[str]
    ledger: Any  # a Ledger or None
    recent_fills: list[dict[str, Any]]


@dataclass
class ComplianceReviewReport:
    org_id: str
    date: str
    recommendation: str
    tokens_in: int = 0
    tokens_out: int = 0
    context_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def _summarize_mandate(m: StrategyMandate) -> str:
    pos = "none"
    realized = "0"
    if m.ledger is not None:
        pos = ", ".join(
            f"{p.symbol} x{p.quantity}"
            for p in m.ledger.open_positions
        ) or "none"
        realized = str(m.ledger.realized_pnl)
    syms = ", ".join(m.declared_symbols) or "(dynamic selection)"
    fills_preview = "\n".join(
        f"  - ts={int(f.get('ts', 0))} {f.get('fill_json', '')}"
        for f in m.recent_fills[:10]
    ) or "  _no recent fills_"
    return (
        f"### {m.name} ({m.bot_id})\n\n"
        f"**Declared symbols:** {syms}\n\n"
        f"**Declared mandate (strategy prompt):**\n\n"
        f"{m.prompt_markdown.strip()}\n\n"
        f"**Observed state:** open positions: {pos}; "
        f"realized PnL: ${realized}\n\n"
        f"**Recent fills:**\n{fills_preview}\n"
    )


def build_context(
    org_id: str,
    strategies: list[StrategyMandate],
    prior_recommendations: str,
) -> str:
    """Assemble the reviewer's prompt context."""

    parts: list[str] = [
        f"# Organization: {org_id}",
        "",
        "# Prior compliance recommendations",
        prior_recommendations.strip() or "_no prior recommendations yet_",
        "",
        "# Strategies under review",
    ]
    if not strategies:
        parts.append("_no strategies in this org to review_")
    else:
        for m in strategies:
            parts.append("")
            parts.append(_summarize_mandate(m))
    return "\n".join(parts)


def run_compliance_review(
    *,
    org_id: str,
    memory_store: Any,
    strategies: list[StrategyMandate],
    llm_invoker: Any,
) -> ComplianceReviewReport:
    """Run one Compliance Agent invocation for an org."""

    date = _today_utc()
    report = ComplianceReviewReport(org_id=org_id, date=date, recommendation="")

    if not strategies:
        report.skipped_reason = "no strategies"
        logger.info("compliance.skipped org_id=%s reason=no_strategies", org_id)
        return report

    try:
        prior = memory_store.read_recommendations()
    except Exception as exc:
        report.errors.append(f"memory.read_recommendations: {exc}")
        prior = ""

    user_prompt = build_context(
        org_id=org_id, strategies=strategies, prior_recommendations=prior,
    )
    report.context_bytes = len(user_prompt.encode("utf-8"))

    try:
        text, tokens_in, tokens_out = llm_invoker(
            COMPLIANCE_SYSTEM_PROMPT, user_prompt,
        )
    except Exception as exc:
        report.errors.append(f"llm_invoker: {exc}")
        return report

    report.tokens_in = tokens_in
    report.tokens_out = tokens_out
    report.recommendation = text.strip()

    try:
        entry = (
            f"\n## {date} — compliance review\n\n{report.recommendation}\n"
        )
        memory_store.append_recommendation(entry)
    except Exception as exc:
        report.errors.append(f"memory.append_recommendation: {exc}")

    logger.info(
        "compliance.complete org_id=%s strategies=%d tokens_in=%d tokens_out=%d",
        org_id, len(strategies), tokens_in, tokens_out,
    )
    return report
