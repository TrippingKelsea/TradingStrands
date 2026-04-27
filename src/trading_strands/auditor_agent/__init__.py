"""Auditor Agent — agentified version of v0 Reconciler.

Reconciles the durable ledger against broker-reported positions, has
authority to halt the desk if drift is observed. The halt signal flows
through the same CONTROL row the operator /api/halt endpoint uses;
the hot path sees it the same regardless of source.
"""

from trading_strands.auditor_agent.runner import (
    AUDITOR_SYSTEM_PROMPT,
    AuditReviewReport,
    HaltControl,
    build_context,
    decide_halt,
    run_audit_review,
)

__all__ = [
    "AUDITOR_SYSTEM_PROMPT",
    "AuditReviewReport",
    "HaltControl",
    "build_context",
    "decide_halt",
    "run_audit_review",
]
