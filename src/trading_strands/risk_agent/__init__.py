"""Risk Agent — per-org periodic LLM reviewer.

Reads the org's fleet of ledgers + recent fills; writes human-
consumable recommendations to recommendations.md. Does NOT gate
individual trades — that's the deterministic Risk Manager's job in
the hot path.
"""

from trading_strands.risk_agent.runner import (
    RISK_SYSTEM_PROMPT,
    RiskReviewReport,
    build_context,
    run_risk_review,
    summarize_fleet,
)

__all__ = [
    "RISK_SYSTEM_PROMPT",
    "RiskReviewReport",
    "build_context",
    "run_risk_review",
    "summarize_fleet",
]
