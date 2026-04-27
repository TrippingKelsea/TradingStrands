"""Tool-quota storage.

Schema:
    pk = TOOL_QUOTA#<strategy_id>#<yyyy-mm-dd>
    {<tool_name>_consumed: N, <tool_name>_cache_hits: N, ..., ttl: <epoch>}

One row per (strategy, day). All tool counters for that strategy-day
live in the same item, bumped via UpdateItem ADD so concurrent increments
from parallel strategy ticks are atomic. TTL = day + 30d so operators
can still inspect yesterday's usage.

Reserve-before-call semantics: increment the counter first; if it would
exceed the limit, the ADD still lands but we raise. This is intentional —
we want the row to exist even at the moment of rejection so operators
see the attempted calls, not just the accepted ones. A separate counter
`<tool>_denied` tracks the denials.
"""

from __future__ import annotations

import time
from typing import Any

# Same 30-day post-day retention LEDGER_EVENT / TOKEN uses.
_COUNTER_TTL_SECONDS = 30 * 24 * 3600


class QuotaExceeded(Exception):
    """Raised when a strategy's daily quota for a tool is exhausted.

    Carries the tool name and strategy_id so the Strands agent layer
    (and observability) can surface meaningful context. Tools wrap
    this as their failure path — the LLM sees the exception and
    typically reasons about it (hold, defer, skip)."""

    def __init__(
        self, strategy_id: str, tool_name: str, consumed: int, limit: int,
    ) -> None:
        super().__init__(
            f"tool quota exceeded: strategy={strategy_id} "
            f"tool={tool_name} consumed={consumed} limit={limit}",
        )
        self.strategy_id = strategy_id
        self.tool_name = tool_name
        self.consumed = consumed
        self.limit = limit


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def _pk(strategy_id: str, date: str) -> str:
    return f"TOOL_QUOTA#{strategy_id}#{date}"


class ToolQuotaStore:
    """Read + reserve quota for (strategy, tool, day)."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def reserve(
        self,
        strategy_id: str,
        tool_name: str,
        limit: int,
        _today: str | None = None,
    ) -> None:
        """Reserve one unit of quota. Raises QuotaExceeded if the
        post-reserve consumed count would exceed the limit.

        limit=0 is always denied — matches the spec's "0 = disabled"
        convention for StrategyToolConfig.daily_quota.

        `_today` is a test seam so day-rollover tests can pin the date
        without mocking time.time globally.
        """

        if limit <= 0:
            raise QuotaExceeded(strategy_id, tool_name, 0, limit)

        date = _today or _today_utc()
        consumed_attr = f"{tool_name}_consumed"
        # Use UpdateItem's ADD so concurrent ticks can both reserve
        # atomically. ReturnValues=UPDATED_NEW gives the post-increment
        # value which we compare to the limit.
        resp = self._table.update_item(
            Key={"pk": _pk(strategy_id, date)},
            UpdateExpression=(
                "ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl)"
            ),
            ExpressionAttributeNames={
                "#c": consumed_attr,
                "#ttl": "ttl",
            },
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": int(time.time()) + _COUNTER_TTL_SECONDS,
            },
            ReturnValues="UPDATED_NEW",
        )
        new_consumed = int(
            resp.get("Attributes", {}).get(consumed_attr, 0),
        )
        if new_consumed > limit:
            # Row already records this attempt — operator can see it
            # as consumed > limit during a post-mortem. Raise so the
            # caller doesn't execute the tool.
            raise QuotaExceeded(
                strategy_id, tool_name, new_consumed, limit,
            )

    def record_cache_hit(
        self,
        strategy_id: str,
        tool_name: str,
        _today: str | None = None,
    ) -> None:
        """Record that a tool call was served from cache without an
        external API round-trip. Does NOT count toward the daily
        quota — this counter is separate and purely for observability
        (cache effectiveness reporting)."""

        date = _today or _today_utc()
        hits_attr = f"{tool_name}_cache_hits"
        self._table.update_item(
            Key={"pk": _pk(strategy_id, date)},
            UpdateExpression=(
                "ADD #h :one SET #ttl = if_not_exists(#ttl, :ttl)"
            ),
            ExpressionAttributeNames={"#h": hits_attr, "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": int(time.time()) + _COUNTER_TTL_SECONDS,
            },
        )

    def consumed_today(
        self,
        strategy_id: str,
        tool_name: str,
        _today: str | None = None,
    ) -> int:
        date = _today or _today_utc()
        resp = self._table.get_item(Key={"pk": _pk(strategy_id, date)})
        item = resp.get("Item") or {}
        return int(item.get(f"{tool_name}_consumed", 0))

    def cache_hits_today(
        self,
        strategy_id: str,
        tool_name: str,
        _today: str | None = None,
    ) -> int:
        date = _today or _today_utc()
        resp = self._table.get_item(Key={"pk": _pk(strategy_id, date)})
        item = resp.get("Item") or {}
        return int(item.get(f"{tool_name}_cache_hits", 0))
