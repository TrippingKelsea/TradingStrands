"""Auditor Agent runner.

Per-org periodic reviewer. Reconciles ledger vs broker positions using
the existing deterministic Reconciler, then hands the result to an
LLM for human-consumable interpretation. Has authority to halt the
desk if the deterministic check fails — the LLM only writes the
narrative; the halt decision is deterministic.

Separation of concerns:
- `decide_halt()`: pure function, (Check) -> bool. Authoritative.
- `run_audit_review()`: orchestrates check + LLM + halt + memory append.
- `HaltControl`: protocol for writing the halt signal — tests pass a
  fake, production passes a DDB-backed writer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import structlog

from trading_strands.auditor.reconciler import (
    AuditConfig,
    AuditResult,
    CheckStatus,
    Reconciler,
)

logger = structlog.get_logger()

AUDITOR_SYSTEM_PROMPT = """\
You are a desk auditor for an automated trading organization. Each
cycle, a deterministic check has already reconciled the durable
ledger against the broker's reported positions; your job is to
write the narrative of what happened for a human operator.

You have halt authority: if the deterministic check failed, the desk
has already been halted by the time you are asked to write. Your
recommendation should explain WHY the desk was halted in specific,
observable terms — "ledger shows AAPL x10 but broker reports x8;
2-share drift since the last reconciliation cycle".

CRITICAL CONSTRAINTS:
- Do NOT propose trades. You audit; you do not act on the market.
- Do NOT question the deterministic check's result — it is the
  source of truth. Your job is to explain and recommend follow-up.
- Do NOT invent causes. Speculate sparingly and mark it as such.
- If the check passed, say so briefly. Operators do not need a
  500-word essay to tell them "desk reconciled".
- If the check failed, be specific: the symbol(s), direction of
  drift, size. Suggest what the orgadmin might investigate first
  (recent trades, broker-side rejections, missed fills).
- Under 400 words.
"""


class HaltControl(Protocol):
    """Protocol for the halt-writing side effect.

    Kept tiny so the Auditor runner doesn't know whether the
    implementation writes to DDB, a config file, or a fake in tests.
    """

    def set_halted(self, halted: bool, reason: str = "") -> None: ...


@dataclass
class AuditReviewReport:
    org_id: str
    date: str
    check_status: str  # "pass" or "fail"
    halt_triggered: bool
    recommendation: str
    tokens_in: int = 0
    tokens_out: int = 0
    context_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def decide_halt(check: AuditResult.Check) -> bool:
    """Deterministic halt rule: any FAIL halts.

    The v0 Reconciler's `should_kill_switch` adds a "consecutive
    cycles" requirement to reduce noise from transient broker
    inconsistencies. The Auditor Agent runs far less often (daily/
    weekly cadence, not per-tick), so a single observed drift is
    itself significant. The LLM cannot override this; its output
    is narrative, not policy.
    """

    return check.status == CheckStatus.FAIL


def _totals_from_ledgers(ledgers: dict[str, Any]) -> dict[str, Decimal]:
    """Aggregate open-position quantities across every bot's ledger."""

    out: dict[str, Decimal] = {}
    for ledger in ledgers.values():
        for pos in ledger.open_positions:
            out[pos.symbol] = out.get(pos.symbol, Decimal("0")) + pos.quantity
    return out


def _totals_from_broker(positions: list[Any]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in positions:
        out[p.symbol] = out.get(p.symbol, Decimal("0")) + p.quantity
    return out


def build_context(
    org_id: str,
    check: AuditResult.Check,
    ledger_totals: dict[str, Decimal],
    broker_totals: dict[str, Decimal],
    prior_recommendations: str,
) -> str:
    """Assemble the prompt context. Always includes the raw totals
    even on PASS — small cost, and the LLM can check that the numbers
    it reports back match."""

    parts: list[str] = [
        f"# Organization: {org_id}",
        "",
        f"# Deterministic check: {check.status.value.upper()}",
    ]
    if check.details:
        parts.append(f"Details: {check.details}")
    parts.extend([
        "",
        "# Ledger totals (aggregated across all active bots)",
    ])
    if not ledger_totals:
        parts.append("_no open positions in any bot's ledger_")
    else:
        for sym, qty in sorted(ledger_totals.items()):
            parts.append(f"- {sym}: ledger={qty}")
    parts.extend([
        "",
        "# Broker-reported totals",
    ])
    if not broker_totals:
        parts.append("_no positions reported by broker_")
    else:
        for sym, qty in sorted(broker_totals.items()):
            parts.append(f"- {sym}: broker={qty}")
    parts.extend([
        "",
        "# Prior recommendations",
        prior_recommendations.strip() or "_no prior recommendations yet_",
    ])
    return "\n".join(parts)


def run_audit_review(
    *,
    org_id: str,
    memory_store: Any,
    halt_control: HaltControl,
    ledgers: dict[str, Any],
    broker_positions: list[Any],
    llm_invoker: Any,
    audit_config: AuditConfig | None = None,
    recommendations_store: Any | None = None,
) -> AuditReviewReport:
    """Run one Auditor Agent invocation.

    Ordering matters. The deterministic check runs first; if it
    fails, the halt is written BEFORE the LLM call so a slow or
    failing LLM can't leave the desk unprotected. Recommendation
    append happens last.
    """

    date = _today_utc()
    cfg = audit_config or AuditConfig()
    reconciler = Reconciler(cfg)
    check = reconciler.reconcile_positions(ledgers, broker_positions)
    check_status = "pass" if check.status == CheckStatus.PASS else "fail"

    report = AuditReviewReport(
        org_id=org_id, date=date,
        check_status=check_status, halt_triggered=False,
        recommendation="",
    )

    # Deterministic halt (ahead of the LLM). The intent is recorded
    # on the report even if the write itself fails, so the operator
    # can tell the Auditor *tried* to halt and needs manual action.
    if decide_halt(check):
        report.halt_triggered = True
        try:
            halt_control.set_halted(
                True,
                reason=f"auditor-agent: drift in {org_id} — {check.details}",
            )
        except Exception as exc:
            report.errors.append(f"halt_control: {exc}")
            logger.exception("auditor.halt_write_failed org_id=%s", org_id)

    try:
        prior = memory_store.read_recommendations()
    except Exception as exc:
        report.errors.append(f"memory.read_recommendations: {exc}")
        prior = ""

    ledger_totals = _totals_from_ledgers(ledgers)
    broker_totals = _totals_from_broker(broker_positions)

    user_prompt = build_context(
        org_id=org_id,
        check=check,
        ledger_totals=ledger_totals,
        broker_totals=broker_totals,
        prior_recommendations=prior,
    )
    report.context_bytes = len(user_prompt.encode("utf-8"))

    try:
        text, tokens_in, tokens_out = llm_invoker(
            AUDITOR_SYSTEM_PROMPT, user_prompt,
        )
        report.tokens_in = tokens_in
        report.tokens_out = tokens_out
        report.recommendation = text.strip()
    except Exception as exc:
        report.errors.append(f"llm_invoker: {exc}")
        # Fall through — the halt (if any) has already fired; the
        # operator will see the missing narrative but also the halt.

    if report.recommendation:
        try:
            halt_marker = " [HALTED]" if report.halt_triggered else ""
            entry = (
                f"\n## {date} — auditor review{halt_marker}\n\n"
                f"**Check:** {check.status.value}"
            )
            if check.details:
                entry += f" — {check.details}"
            entry += f"\n\n{report.recommendation}\n"
            memory_store.append_recommendation(entry)
        except Exception as exc:
            report.errors.append(f"memory.append_recommendation: {exc}")

    # Auditor severity ties to whether the deterministic check tripped
    # halt — if the desk was halted by this pass, the advisory is
    # categorically more urgent than an LLM-suggested future concern.
    if recommendations_store is not None and report.recommendation:
        severity = "critical" if report.halt_triggered else "info"
        summary = (
            f"[HALT] {report.recommendation[:180]}"
            if report.halt_triggered
            else report.recommendation[:200]
        )
        try:
            recommendations_store.append(
                org_id=org_id,
                agent_type="auditor",
                agent_id=f"auditor-{org_id}",
                severity=severity,
                summary=summary,
                body=report.recommendation,
            )
        except Exception as exc:
            report.errors.append(f"recommendations_store.append: {exc}")

    logger.info(
        "auditor.complete org_id=%s status=%s halt=%s",
        org_id, check_status, report.halt_triggered,
    )
    return report
