"""Strategy persistence — org-scoped, author-attributed, ACL-aware.

All strategy items in DynamoDB now carry two new required attributes:

    org_id          — the organization that owns this strategy.
    author_user_id  — the user who created it.

Plus an optional `STRATEGYACL#{strategy_id}#{user_id}` item set for
delegated co-authors who can edit the strategy.

Queries in this module never return strategies across org boundaries
without an explicit caller-supplied org filter. That filter is the
single place we enforce cross-org privacy at the persistence layer.
HTTP-layer authorization is layered ON TOP of this — both checks must
pass.
"""

from trading_strands.strategies_store.store import (
    Strategy,
    StrategyACL,
    StrategyStatus,
    StrategyStore,
)

__all__ = [
    "Strategy",
    "StrategyACL",
    "StrategyStatus",
    "StrategyStore",
]
