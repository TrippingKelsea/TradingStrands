"""End-of-day batch memory compactor.

Per docs/SPEC/agent_memory.md §"Batch compaction (end-of-day)":
reads a Strategy Agent's raw YYYY-MM-DD.md, writes a compressed
sibling optimized for downstream readers (tomorrow-the-agent,
weekend self-critique, chat). Idempotent — re-running rewrites
the compressed file; the raw file is never destroyed (audit
requirement).

This is categorically separate from *live* compaction (which the
Strategy Agent does inside its own context window during the day).
Live compaction targets "what's relevant to my next tick"; batch
compaction targets "what's useful to a week-later reader", and can
use a larger model + longer time budget.
"""

from __future__ import annotations

from trading_strands.memory_compactor.runner import (
    COMPACTOR_SYSTEM_PROMPT,
    CompactReport,
    run_compact_day,
)

__all__ = [
    "COMPACTOR_SYSTEM_PROMPT",
    "CompactReport",
    "run_compact_day",
]
