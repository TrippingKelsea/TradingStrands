"""Per-Agent durable memory.

See docs/SPEC/agent_memory.md for the full design. TL;DR:

  - Each Agent has its own scoped region of S3, containing daily markdown
    memory files + an append-only lessons.md.
  - Agents write to today's file as they reason, and downstream readers
    (chat feature, Self-Critique Agent, auditor) pull days on demand.
  - Every concrete market claim in memory must carry a DDB pointer back
    to the raw market-data bar it references; the Agent's system prompt
    is the enforcement mechanism for this discipline.

v0 scoping: one shared platform bucket partitioned by prefix
(`{org_id}/{agent_type}/{agent_id}/...`). When BotProvisioner lands
(Gate B), switches to per-Agent dedicated buckets. The store interface
is deliberately agnostic — only the path resolution knows.
"""

from trading_strands.agent_memory.store import (
    AgentMemoryStore,
    DailyFileKey,
    today_utc,
)

__all__ = ["AgentMemoryStore", "DailyFileKey", "today_utc"]
