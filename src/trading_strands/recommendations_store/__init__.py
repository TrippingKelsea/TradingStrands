"""Cross-agent recommendation aggregation for Org Advisories.

Risk, Compliance, and Auditor agents each write their own
recommendations.md in their per-agent S3 memory. That's useful for an
operator drilling into a specific review but terrible for "what
should this org's owner look at right now?" — they'd have to read
three files across three agent types.

This module surfaces a single DDB row per advisory:

    RECOMMENDATION#{org_id}#{unix_ts}#{agent_type}

with `severity`, `summary`, `agent_type`, `body`, `created_at`, plus
a short TTL so old recs self-prune. Every review agent calls
`append_recommendation` on its own memory bucket AND publishes a
RecommendationEntry here. The dashboard reads by org prefix; the
agent-memory copy is retained for the agent's own next-run context.
"""

from __future__ import annotations

from trading_strands.recommendations_store.store import (
    RecommendationEntry,
    RecommendationSeverity,
    RecommendationsStore,
)

__all__ = [
    "RecommendationEntry",
    "RecommendationSeverity",
    "RecommendationsStore",
]
