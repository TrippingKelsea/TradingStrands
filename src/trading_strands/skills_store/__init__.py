"""Per-org skills: reusable prompt fragments composed into a
strategy bot's system prompt at bot-start.

See docs/SPEC/tools.md §8. Text-only, no external calls, no quota.
Orgadmin-authored, per-org-scoped. Missing skills at bot-start are
logged and skipped — skills are enhancement, not load-bearing.
"""

from trading_strands.skills_store.store import (
    Skill,
    SkillNotFoundError,
    SkillsStore,
    SkillTooLargeError,
    compose_system_prompt,
)

__all__ = [
    "Skill",
    "SkillNotFoundError",
    "SkillTooLargeError",
    "SkillsStore",
    "compose_system_prompt",
]
