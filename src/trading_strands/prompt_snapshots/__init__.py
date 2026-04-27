"""Last-rendered prompt per strategy bot.

Backs the detail page's Prompt tab. A single row per bot
(`PROMPTSNAPSHOT#{bot_id}`) is overwritten each tick; operators
inspect it to see exactly what the LLM saw on the most recent
decide(). History isn't kept — the memory file is the audit trail,
the snapshot is a live view.
"""

from __future__ import annotations

from trading_strands.prompt_snapshots.store import (
    PromptSnapshot,
    PromptSnapshotStore,
)

__all__ = [
    "PromptSnapshot",
    "PromptSnapshotStore",
]
