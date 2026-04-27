"""Per-strategy per-day tool-call quota accounting.

Counts count (§7 of SPEC/tools.md). Used by tool wrappers to enforce
hard-stop daily budgets. A failed external call still counts (quota
is incremented BEFORE the call runs). Cache hits don't count; they're
tracked separately for observability.
"""

from trading_strands.tool_quota.store import (
    QuotaExceeded,
    ToolQuotaStore,
)

__all__ = ["QuotaExceeded", "ToolQuotaStore"]
