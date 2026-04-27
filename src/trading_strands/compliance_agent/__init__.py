"""Compliance Agent — per-org strategy drift reviewer.

Reads each active strategy's declared mandate (its prompt) + its
observed activity and flags drift. Output is recommendations for the
orgadmin; the Compliance Agent does not mutate strategies.
"""

from trading_strands.compliance_agent.runner import (
    COMPLIANCE_SYSTEM_PROMPT,
    ComplianceReviewReport,
    StrategyMandate,
    build_context,
    run_compliance_review,
)

__all__ = [
    "COMPLIANCE_SYSTEM_PROMPT",
    "ComplianceReviewReport",
    "StrategyMandate",
    "build_context",
    "run_compliance_review",
]
