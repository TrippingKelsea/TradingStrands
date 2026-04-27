"""Tests for FilingsIndexStore + FilingsBodyStore."""

from __future__ import annotations

import time
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.filings_store.stores import (
    FilingIndex,
    FilingsBodyStore,
    FilingsIndexStore,
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


# ── Index store ────────────────────────────────────────────────────


def test_index_put_and_get_round_trip() -> None:
    with mock_aws():
        store = FilingsIndexStore(_table())
        filing = FilingIndex(
            ticker="AAPL",
            accession="0000320193-26-000001",
            form_type="8-K",
            filing_date="2026-04-27",
            summary="Material event: new product",
            body_s3_key="AAPL/8-K/0000320193-26-000001.html",
            indexed_at=int(time.time()),
        )
        store.put(filing)

        loaded = store.get("AAPL", "0000320193-26-000001")
        assert loaded is not None
        assert loaded.form_type == "8-K"
        assert loaded.filing_date == "2026-04-27"
        assert loaded.body_s3_key.startswith("AAPL/")


def test_index_list_for_ticker_filters() -> None:
    """list_for_ticker returns newest first, bounded by days_back."""

    with mock_aws():
        store = FilingsIndexStore(_table())
        now = int(time.time())
        store.put(FilingIndex(
            ticker="AAPL", accession="A", form_type="8-K",
            filing_date="2026-04-27", summary="recent",
            body_s3_key="k", indexed_at=now,
        ))
        store.put(FilingIndex(
            ticker="AAPL", accession="B", form_type="10-Q",
            filing_date="2026-02-01", summary="older",
            body_s3_key="k2", indexed_at=now - 60 * 86400,
        ))
        # Different ticker — must not appear.
        store.put(FilingIndex(
            ticker="MSFT", accession="C", form_type="8-K",
            filing_date="2026-04-27", summary="other",
            body_s3_key="k3", indexed_at=now,
        ))

        recent = store.list_for_ticker("AAPL", form_types=None, days_back=14)
        accessions = [f.accession for f in recent]
        assert accessions == ["A"]   # newer only; "B" is older than 14d

        all_aapl = store.list_for_ticker(
            "AAPL", form_types=None, days_back=365,
        )
        # Newest first.
        assert [f.accession for f in all_aapl] == ["A", "B"]


def test_index_list_filters_by_form_type() -> None:
    with mock_aws():
        store = FilingsIndexStore(_table())
        now = int(time.time())
        store.put(FilingIndex(
            ticker="AAPL", accession="A", form_type="8-K",
            filing_date="2026-04-27", summary="",
            body_s3_key="k", indexed_at=now,
        ))
        store.put(FilingIndex(
            ticker="AAPL", accession="B", form_type="4",
            filing_date="2026-04-27", summary="",
            body_s3_key="k", indexed_at=now,
        ))

        eights = store.list_for_ticker(
            "AAPL", form_types=["8-K"], days_back=30,
        )
        assert [f.accession for f in eights] == ["A"]


def test_index_put_is_idempotent() -> None:
    """Re-running the watcher on the same accession overwrites
    cleanly — no dup rows."""

    with mock_aws():
        table = _table()
        store = FilingsIndexStore(table)
        filing = FilingIndex(
            ticker="AAPL", accession="A", form_type="8-K",
            filing_date="2026-04-27", summary="v1",
            body_s3_key="k", indexed_at=int(time.time()),
        )
        store.put(filing)
        store.put(filing)   # same key, no error
        resp = table.scan()
        assert len(resp.get("Items", [])) == 1


# ── Body store ─────────────────────────────────────────────────────


def test_body_put_and_get() -> None:
    with mock_aws():
        _bucket()
        body_store = FilingsBodyStore(
            s3_client=boto3.client("s3", region_name="us-west-2"),
            bucket="ts-filings-test",
        )
        body_store.put("AAPL/8-K/0000320193-26-000001.html", "<html>...</html>")
        loaded = body_store.get("AAPL/8-K/0000320193-26-000001.html")
        assert loaded is not None
        assert "html" in loaded


def test_body_get_missing_returns_none() -> None:
    with mock_aws():
        _bucket()
        body_store = FilingsBodyStore(
            s3_client=boto3.client("s3", region_name="us-west-2"),
            bucket="ts-filings-test",
        )
        assert body_store.get("missing/key.html") is None


def test_body_get_with_truncation() -> None:
    """The tool truncates filings before handing them to the LLM —
    an 8-K can be 500 KB, and the token cost makes that wasteful.
    Truncate at the store layer so the contract is explicit."""

    with mock_aws():
        _bucket()
        body_store = FilingsBodyStore(
            s3_client=boto3.client("s3", region_name="us-west-2"),
            bucket="ts-filings-test",
        )
        body_store.put("k", "x" * 1000)
        loaded = body_store.get("k", max_bytes=100)
        assert loaded is not None
        assert len(loaded) == 100


def test_build_body_key() -> None:
    """Canonical key derivation — keeps watcher and reader in sync."""

    from trading_strands.filings_store.stores import build_body_key

    assert build_body_key(
        "AAPL", "8-K", "0000320193-26-000001",
    ) == "AAPL/8-K/0000320193-26-000001.html"
    # Tickers are uppercased.
    assert build_body_key(
        "aapl", "10-Q", "0000320193-26-000002",
    ) == "AAPL/10-Q/0000320193-26-000002.html"
