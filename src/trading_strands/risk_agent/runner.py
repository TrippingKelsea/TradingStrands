"""Risk Agent runner.

Pure-function core. The runner:
    1. Loads prior recommendations (read-only history).
    2. Summarizes the org's fleet of ledgers (current positions, realized
       PnL, drawdown from high-water mark).
    3. Assembles a context prompt, calls the LLM.
    4. Appends the response as a dated recommendation block.

Shape mirrors Self-Critique so the Lambda glue stays minimal and
operators can reason about both agents from the same mental model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()

RISK_SYSTEM_PROMPT = """\
You are an organization-level risk reviewer for an automated trading
platform. You review the org's fleet of strategies periodically and
produce short, actionable recommendations for a human orgadmin.

Your scope:
- Concentration risk across the fleet (positions that are large
  fractions of total equity, or overlap across multiple strategies).
- Drawdown trajectory — is any strategy repeatedly making new lows?
- Pattern risk — cross-strategy correlation, signs that two "independent"
  strategies are taking the same trade for the same reason.

CRITICAL CONSTRAINTS:
- Do NOT propose trades. You are a reviewer, not a strategy.
- Do NOT attempt to change the deterministic risk manager's config.
  The deterministic code in the hot path stays authoritative; your
  recommendations are suggestions the orgadmin evaluates.
- Do NOT invent market data. Reason only from what's provided in the
  context. If you need something that isn't there, say so.
- Keep recommendations under 400 words. Short, specific, actionable.
- Frame each recommendation as: what you observed, why it's a concern,
  what the orgadmin might consider. The orgadmin decides.
"""


@dataclass
class RiskReviewReport:
    org_id: str
    date: str  # YYYY-MM-DD
    recommendation: str  # markdown body
    tokens_in: int = 0
    tokens_out: int = 0
    context_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def summarize_fleet(ledgers: dict[str, Any]) -> str:
    """Roll up the org's ledgers into one markdown block.

    Risk reasoning needs cross-bot visibility: a position that looks
    fine in isolation may be the same trade a sister strategy already
    holds, doubling the org's exposure. Summaries stay markdown so a
    human reader can skim the same text the LLM sees.
    """

    if not ledgers:
        return "_no bots in this org_"

    total_start = Decimal("0")
    total_realized = Decimal("0")
    lines: list[str] = []
    for bot_id, ledger in ledgers.items():
        total_start += Decimal(str(ledger.starting_capital))
        total_realized += Decimal(str(ledger.realized_pnl))
        pos = ", ".join(
            f"{p.symbol} x{p.quantity} @ ${p.burdened_cost_basis}"
            for p in ledger.open_positions
        ) or "none"
        lines.append(
            f"- **{bot_id}**: start ${ledger.starting_capital}, "
            f"realized ${ledger.realized_pnl}, "
            f"hwm ${ledger.high_water_mark}, open: {pos}",
        )

    header = (
        f"Fleet total starting capital: ${total_start}. "
        f"Total realized PnL: ${total_realized}.\n"
    )
    return header + "\n".join(lines)


def build_context(
    org_id: str,
    fleet_summary: str,
    prior_recommendations: str,
    recent_fills: list[dict[str, Any]],
) -> str:
    """Assemble the prompt context for the Risk Agent.

    Recent fills give the LLM a feel for activity intensity — a calm
    week vs a flurry — without burying the model in full event detail.
    """

    parts: list[str] = [
        f"# Organization: {org_id}",
        "",
        "# Fleet summary",
        fleet_summary.strip(),
        "",
        "# Prior recommendations",
        prior_recommendations.strip() or "_no prior recommendations yet_",
        "",
        "# Recent fills (newest first)",
    ]
    if not recent_fills:
        parts.append("_no recent fills in the review window_")
    else:
        for fill in recent_fills[:50]:
            ts = int(fill.get("ts", 0))
            body = str(fill.get("fill_json", ""))
            parts.append(f"- ts={ts} {body}")
    return "\n".join(parts)


def run_risk_review(
    *,
    org_id: str,
    memory_store: Any,
    ledgers: dict[str, Any],
    recent_fills: list[dict[str, Any]],
    llm_invoker: Any,
    recommendations_store: Any | None = None,
) -> RiskReviewReport:
    """Run one Risk Agent invocation for an org.

    `llm_invoker` shape is `(system, user) -> (text, tokens_in, tokens_out)`
    matching Self-Critique. Failures produce a report with errors set
    and an empty recommendation — callers decide whether to retry or
    alert.

    `recommendations_store` is the cross-agent aggregator; when set,
    the runner also publishes a RECOMMENDATION#* entry so the
    dashboard's Org Advisories endpoint sees this review alongside
    Compliance/Auditor output. Optional for back-compat with existing
    callers and local-dev (None = S3 recommendations.md only).
    """

    date = _today_utc()
    report = RiskReviewReport(org_id=org_id, date=date, recommendation="")

    try:
        prior = memory_store.read_recommendations()
    except Exception as exc:
        report.errors.append(f"memory.read_recommendations: {exc}")
        prior = ""

    fleet_summary = summarize_fleet(ledgers)
    user_prompt = build_context(
        org_id=org_id,
        fleet_summary=fleet_summary,
        prior_recommendations=prior,
        recent_fills=recent_fills,
    )
    report.context_bytes = len(user_prompt.encode("utf-8"))

    try:
        response_text, tokens_in, tokens_out = llm_invoker(
            RISK_SYSTEM_PROMPT, user_prompt,
        )
    except Exception as exc:
        report.errors.append(f"llm_invoker: {exc}")
        return report

    report.tokens_in = tokens_in
    report.tokens_out = tokens_out
    report.recommendation = response_text.strip()

    try:
        entry = (
            f"\n## {date} — risk review\n\n{report.recommendation}\n"
        )
        memory_store.append_recommendation(entry)
    except Exception as exc:
        report.errors.append(f"memory.append_recommendation: {exc}")

    if recommendations_store is not None:
        try:
            recommendations_store.append(
                org_id=org_id,
                agent_type="risk",
                agent_id=f"risk-{org_id}",
                severity="info",
                summary=(report.recommendation or "")[:200],
                body=report.recommendation,
            )
        except Exception as exc:
            report.errors.append(f"recommendations_store.append: {exc}")

    logger.info(
        "risk_agent.complete org_id=%s tokens_in=%d tokens_out=%d",
        org_id, tokens_in, tokens_out,
    )
    return report
