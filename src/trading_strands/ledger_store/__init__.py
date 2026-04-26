"""Durable ledger storage.

The in-memory Ledger (ledger/models.py) is the source of truth during a
bot's life. This module persists it to DynamoDB so that:

  - Bot restarts (deploys, Fargate task replacement, the scale-down
    scheduler's nightly cycle) resume from the prior state instead of
    reconstituting from zero every morning.
  - The Auditor has a durable record to reconcile against the broker.
  - Chat / Self-Critique features can reference exact ledger state at
    any historical decision.

Schema:
    LEDGER#{bot_id}                   — current full ledger snapshot, overwritten
                                        on each fill. Kept indefinitely (small
                                        item; the audit log is the event stream).

    LEDGER_EVENT#{bot_id}#{ts-rand}   — one item per fill, append-only.
                                        ttl=90 days (configurable). These
                                        are the audit trail; the snapshot
                                        is a read-perf optimization over
                                        replaying them.

The snapshot is overwritten, NOT versioned. Point-in-time recovery uses
the event log; the snapshot is "the latest state" for fast reads.
"""

from trading_strands.ledger_store.store import LedgerStore

__all__ = ["LedgerStore"]
