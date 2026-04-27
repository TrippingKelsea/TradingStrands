"""SEC filings tool — read-only cache tool.

Different cost model from `news`: this tool NEVER hits EDGAR. The
EDGAR watcher Lambda is the sole writer. So every call is a cache
read, and `cache_hit` is the only success outcome.

Quota is still enforced as a rate-limit safety rail (a strategy
calling list_filings in a tight loop is a bug, not a feature), but
the primary quota-exhaustion outcome for filings is "strategy is
misbehaving" rather than "we're burning cost at the API provider".

Two tools:
  - list_recent_filings(symbol, form_types, days_back) → list of
    index rows (metadata only — summary, not body).
  - read_filing(accession) → body text, truncated to max_bytes.
"""

from __future__ import annotations

from typing import Any

import structlog

from trading_strands.filings_store.stores import (
    FilingsBodyStore,
    FilingsIndexStore,
)
from trading_strands.tool_quota.store import QuotaExceeded
from trading_strands.tools.base import ToolContext
from trading_strands.tools.observability import emit_tool_outcome

logger = structlog.get_logger()

_DEFAULT_READ_MAX_BYTES = 32 * 1024


def _rate_limit_or_raise(
    ctx: ToolContext, tool: str, daily_quota: int,
) -> None:
    """Rate-limit protection for pure-cache tools.

    §7.3 says cache hits don't consume quota. But a strategy calling
    list_filings in a tight loop is still pathological — we want a
    brake. Solution: count cache_hits_today against the daily_quota
    as a separate rate ceiling. Consumed counter stays at 0 for
    forensic clarity (operators can tell which tools made external
    calls vs pure-cache reads), while cache_hits_today remains
    bounded to catch runaway loops.
    """

    hits = ctx.quota_store.cache_hits_today(ctx.strategy_id, tool)
    if daily_quota <= 0 or hits >= daily_quota:
        raise QuotaExceeded(
            ctx.strategy_id, tool, hits, daily_quota,
        )


def _run_list_filings(
    ctx: ToolContext,
    symbol: str,
    form_types: list[str] | None,
    days_back: int,
    daily_quota: int,
) -> list[dict[str, Any]]:
    """Return cached filing metadata for a symbol."""

    sym = symbol.upper()

    try:
        _rate_limit_or_raise(ctx, "filings", daily_quota)
    except QuotaExceeded:
        emit_tool_outcome(
            "filings", "quota_exceeded",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        raise
    ctx.quota_store.record_cache_hit(ctx.strategy_id, "filings")

    idx = FilingsIndexStore(ctx.table)
    filings = idx.list_for_ticker(
        sym, form_types=form_types, days_back=days_back,
    )
    emit_tool_outcome(
        "filings", "cache_hit",
        strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
    )
    return [
        {
            "accession": f.accession,
            "form_type": f.form_type,
            "filing_date": f.filing_date,
            "summary": f.summary,
        }
        for f in filings
    ]


def _find_filing_body_key(
    table: Any, accession: str,
) -> tuple[str | None, str | None]:
    """Locate a filing index row by accession alone (ticker
    unknown). Scans the FILING_INDEX# prefix and matches on the
    accession column. Returns (body_s3_key, ticker) or (None, None)."""

    from boto3.dynamodb.conditions import Attr

    resp = table.scan(
        FilterExpression=Attr("pk").begins_with("FILING_INDEX#")
        & Attr("accession").eq(accession),
    )
    items = resp.get("Items", [])
    if not items:
        return None, None
    item = items[0]
    return str(item["body_s3_key"]), str(item["ticker"])


def _run_read_filing(
    ctx: ToolContext,
    accession: str,
    max_bytes: int = _DEFAULT_READ_MAX_BYTES,
    daily_quota: int = 10,
) -> str | None:
    """Return the filing body, truncated to max_bytes."""

    try:
        _rate_limit_or_raise(ctx, "filings", daily_quota)
    except QuotaExceeded:
        emit_tool_outcome(
            "filings", "quota_exceeded",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id,
        )
        raise
    ctx.quota_store.record_cache_hit(ctx.strategy_id, "filings")

    body_key, _ticker = _find_filing_body_key(ctx.table, accession)
    if body_key is None:
        emit_tool_outcome(
            "filings", "not_found",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id,
        )
        return None

    import boto3

    body_store = FilingsBodyStore(
        s3_client=boto3.client("s3"),
        # Reading the bucket name from env rather than plumbing it
        # through the context — tools rarely need bucket identity;
        # this keeps ToolContext from growing per-tool surface.
        bucket=_filings_bucket(),
    )
    body = body_store.get(body_key, max_bytes=max_bytes)
    emit_tool_outcome(
        "filings", "cache_hit" if body else "not_found",
        strategy_id=ctx.strategy_id, org_id=ctx.org_id,
    )
    return body


def _filings_bucket() -> str:
    """Env lookup deferred to call time so tests that mock the S3
    client don't need to set FILINGS_BUCKET."""

    import os
    return os.environ.get("FILINGS_BUCKET", "ts-filings-test")


def make_filings_tools(
    ctx: ToolContext, daily_quota: int = 50,
) -> Any:
    """Factory returning the two @tool-decorated callables."""

    from strands import tool

    @tool
    def list_recent_filings(
        symbol: str,
        form_types: list[str] | None = None,
        days_back: int = 14,
    ) -> list[dict[str, Any]]:
        """List recent SEC filings for a symbol.

        Returns metadata (form_type, filing_date, accession, summary)
        from the cache populated by the EDGAR watcher. Does NOT
        hit EDGAR — rate-limit discipline means strategies read
        from cache only.

        form_types defaults to 8-K + Form 4 when None.
        """

        return _run_list_filings(
            ctx=ctx, symbol=symbol,
            form_types=form_types, days_back=days_back,
            daily_quota=daily_quota,
        )

    @tool
    def read_filing(accession: str, max_bytes: int = 32768) -> str | None:
        """Read a filing's body by accession number. Result is
        truncated to max_bytes (default 32 KB). Returns None if
        the accession isn't in the cache."""

        return _run_read_filing(
            ctx=ctx, accession=accession,
            max_bytes=max_bytes, daily_quota=daily_quota,
        )

    # Strands' `tools=` kwarg takes a list — return both.
    return [list_recent_filings, read_filing]
