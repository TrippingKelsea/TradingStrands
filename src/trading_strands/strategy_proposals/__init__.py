"""Self-critique proposed edits to strategy prompts.

The Self-Critique Agent can suggest a specific change to a strategy's
prompt (not just a lesson). Per docs/SPEC/agents.md §Self-Critique,
those proposals MUST be delivered as a recommendation the author
reviews — never auto-applied — because authorship authority belongs
to the author, not the critique agent.

This module stores proposals and lets the author apply or reject them
from the dashboard. Applied proposals flow through StrategyStore.update
so the normal authz + validate path runs.
"""

from __future__ import annotations

from trading_strands.strategy_proposals.store import (
    ProposalNotFoundError,
    ProposalStatus,
    StrategyProposal,
    StrategyProposalsStore,
)

__all__ = [
    "ProposalNotFoundError",
    "ProposalStatus",
    "StrategyProposal",
    "StrategyProposalsStore",
]
