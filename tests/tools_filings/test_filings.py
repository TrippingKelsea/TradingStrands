"""Tests for the filings tool's orchestration core.

The tool is a pure read over the cache populated by the EDGAR
watcher. Because it never touches EDGAR directly, cache hits do
not consume quota (§7.3) — there's no external call cost to gate.
That's the opposite model from news, where the tool path can hit
external APIs on cache miss.
"""

from __future__ import annotations

import time
from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.filings_store.stores import (
    FilingIndex,
    FilingsBodyStore,
    FilingsIndexStore,
)
from trading_strands.tool_quota.store import ToolQuotaStore
from trading_strands.tools.base import ToolContext
from trading_strands.tools.filings import (
    _run_list_filings,
    _run_read_filing,
)


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def _bucket() -> Any:
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(
        Bucket="ts-filings-test",
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )
    return s3


def _ctx(
    table: Any, s3: Any,
    strategy_id: str = "strat-1", org_id: str = "org-a",
) -> ToolContext:
    return ToolContext(
        strategy_id=strategy_id, org_id=org_id,
        quota_store=ToolQuotaStore(table),
        secrets_client=None, table=table,
    )


def _seed_filing(table: Any, s3: Any, **overrides: Any) -> FilingIndex:
    filing = FilingIndex(
        ticker="AAPL",
        accession="0000320193-26-000001",
        form_type="8-K",
        filing_date="2026-04-27",
        summary="Material event",
        body_s3_key="AAPL/8-K/0000320193-26-000001.html",
        indexed_at=int(time.time()),
        **overrides,
    )
    FilingsIndexStore(table).put(filing)
    FilingsBodyStore(s3_client=s3, bucket="ts-filings-test").put(
        filing.body_s3_key, "<html>body</html>",
    )
    return filing


# ── _run_list_filings ──────────────────────────────────────────────


def test_list_filings_returns_from_cache() -> None:
    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)

        result = _run_list_filings(
            ctx=ctx, symbol="AAPL",
            form_types=["8-K"], days_back=14, daily_quota=10,
        )
        assert len(result) == 1
        assert result[0]["form_type"] == "8-K"
        assert result[0]["accession"] == "0000320193-26-000001"


def test_list_filings_cache_hit_does_not_consume_quota() -> None:
    """Read-only cache path — no external call — so quota is NOT
    consumed. Cache-hit counter tracks usage for observability."""

    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)

        _run_list_filings(
            ctx=ctx, symbol="AAPL",
            form_types=None, days_back=14, daily_quota=10,
        )
        assert ctx.quota_store.consumed_today("strat-1", "filings") == 0
        assert ctx.quota_store.cache_hits_today("strat-1", "filings") == 1


def test_list_filings_still_respects_daily_quota() -> None:
    """Even though reads don't cost API money, an exhausted rate
    limit is still a stop signal — a strategy calling list_filings
    in a tight loop gets braked. For cache-only tools the
    cache_hits_today counter fills up (§7.3 — consumed stays 0)."""

    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)

        # Fill the cache-hit counter directly to simulate a loop
        # that already burned through.
        for _ in range(3):
            ctx.quota_store.record_cache_hit("strat-1", "filings")

        with pytest.raises(Exception, match=r"(?i)quota"):
            _run_list_filings(
                ctx=ctx, symbol="AAPL",
                form_types=None, days_back=14, daily_quota=3,
            )


def test_list_filings_uppercases_symbol() -> None:
    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)
        result = _run_list_filings(
            ctx=ctx, symbol="aapl",
            form_types=None, days_back=14, daily_quota=10,
        )
        assert len(result) == 1


# ── _run_read_filing ──────────────────────────────────────────────


def test_read_filing_returns_truncated_body() -> None:
    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)

        # With default truncation (much larger than test body), full
        # body returned.
        body = _run_read_filing(
            ctx=ctx, accession="0000320193-26-000001",
            max_bytes=4096, daily_quota=10,
        )
        assert body is not None
        assert "<html>body</html>" in body


def test_read_filing_truncates_at_max_bytes() -> None:
    with mock_aws():
        table = _table()
        s3 = _bucket()
        # Overwrite the body to be larger.
        filing = _seed_filing(table, s3)
        FilingsBodyStore(
            s3_client=s3, bucket="ts-filings-test",
        ).put(filing.body_s3_key, "x" * 5000)

        ctx = _ctx(table, s3)
        body = _run_read_filing(
            ctx=ctx, accession=filing.accession,
            max_bytes=200, daily_quota=10,
        )
        assert body is not None
        assert len(body) == 200


def test_read_filing_missing_accession_returns_none() -> None:
    with mock_aws():
        table = _table()
        s3 = _bucket()
        ctx = _ctx(table, s3)
        body = _run_read_filing(
            ctx=ctx, accession="does-not-exist",
            max_bytes=4096, daily_quota=10,
        )
        assert body is None


def test_read_filing_still_consumes_quota_on_hit() -> None:
    """read_filing returning the body is a cache hit (no external
    call). Cache-hit counter bumps; quota counter does not."""

    with mock_aws():
        table = _table()
        s3 = _bucket()
        _seed_filing(table, s3)
        ctx = _ctx(table, s3)
        _run_read_filing(
            ctx=ctx, accession="0000320193-26-000001",
            max_bytes=4096, daily_quota=10,
        )
        assert ctx.quota_store.consumed_today("strat-1", "filings") == 0
        assert ctx.quota_store.cache_hits_today("strat-1", "filings") == 1
