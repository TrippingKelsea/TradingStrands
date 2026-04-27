"""Tests for the EDGAR watcher's pure-function core."""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.edgar_watcher.watcher import (
    build_ticker_to_cik_map,
    parse_submissions_json,
    process_ticker,
)
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


# ── build_ticker_to_cik_map ────────────────────────────────────────


def test_build_ticker_to_cik_map_translates_sec_format() -> None:
    """SEC's ticker-map JSON is a list-of-objects keyed by integer
    strings. We flatten to ticker→CIK (zero-padded 10 digits)."""

    raw = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
    }
    m = build_ticker_to_cik_map(raw)
    assert m["AAPL"] == "0000320193"
    assert m["MSFT"] == "0000789019"


def test_build_map_uppercases_tickers() -> None:
    raw = {"0": {"cik_str": 1, "ticker": "aapl", "title": "X"}}
    m = build_ticker_to_cik_map(raw)
    assert "AAPL" in m
    assert "aapl" not in m


# ── parse_submissions_json ─────────────────────────────────────────


def _submissions_shape(form_types: list[str], accessions: list[str]) -> dict[str, Any]:
    """Minimal shape matching SEC's submissions JSON."""

    n = len(form_types)
    return {
        "filings": {
            "recent": {
                "form": form_types,
                "accessionNumber": accessions,
                "filingDate": ["2026-04-27"] * n,
                "primaryDocument": ["foo.htm"] * n,
            },
        },
    }


def test_parse_filters_to_requested_form_types() -> None:
    raw = _submissions_shape(
        form_types=["8-K", "10-Q", "4", "10-K"],
        accessions=["A", "B", "C", "D"],
    )
    filings = parse_submissions_json(
        raw, ticker="AAPL", form_types=["8-K", "4"], max_count=10,
    )
    forms = [f["form_type"] for f in filings]
    assert forms == ["8-K", "4"]


def test_parse_respects_max_count() -> None:
    raw = _submissions_shape(
        form_types=["8-K"] * 20,
        accessions=[f"A{i}" for i in range(20)],
    )
    filings = parse_submissions_json(
        raw, ticker="AAPL", form_types=["8-K"], max_count=5,
    )
    assert len(filings) == 5


def test_parse_carries_primary_document_url() -> None:
    """process_ticker uses primary_document to construct the body
    fetch URL — must flow through intact."""

    raw = _submissions_shape(
        form_types=["8-K"], accessions=["0000320193-26-000001"],
    )
    raw["filings"]["recent"]["primaryDocument"] = ["earnings.htm"]
    [first] = parse_submissions_json(
        raw, ticker="AAPL", form_types=None, max_count=10,
    )
    assert first["primary_document"] == "earnings.htm"


def test_parse_empty_submissions_is_empty() -> None:
    raw = _submissions_shape(form_types=[], accessions=[])
    assert parse_submissions_json(raw, ticker="X", form_types=None, max_count=10) == []


# ── process_ticker ─────────────────────────────────────────────────


class _StubEdgar:
    """Stand-in for EdgarClient. Records which URLs would be fetched
    and returns canned bodies."""

    def __init__(
        self,
        submissions: dict[str, Any] | None = None,
        bodies: dict[str, str] | None = None,
    ) -> None:
        self._subs = submissions or {"filings": {"recent": {
            "form": [], "accessionNumber": [],
            "filingDate": [], "primaryDocument": [],
        }}}
        self._bodies = bodies or {}
        self.body_fetches: list[str] = []

    def submissions(self, cik: str) -> dict[str, Any]:
        return self._subs

    def filing_body(self, cik: str, accession: str, primary_doc: str) -> str:
        self.body_fetches.append(f"{cik}/{accession}/{primary_doc}")
        return self._bodies.get(accession, "<html>body</html>")


def test_process_ticker_new_filings_land_in_index_and_body() -> None:
    """End-to-end: watcher sees a filing not yet in index, fetches
    body, writes both stores."""

    with mock_aws():
        table = _table()
        _bucket()
        s3 = boto3.client("s3", region_name="us-west-2")
        idx = FilingsIndexStore(table)
        body_store = FilingsBodyStore(s3_client=s3, bucket="ts-filings-test")

        edgar = _StubEdgar(
            submissions=_submissions_shape(
                form_types=["8-K"], accessions=["A-NEW"],
            ),
            bodies={"A-NEW": "<html>new body</html>"},
        )
        n = process_ticker(
            ticker="AAPL", cik="0000320193",
            client=edgar,
            index_store=idx, body_store=body_store,
            form_types=["8-K", "4"],
        )

        assert n == 1
        loaded = idx.get("AAPL", "A-NEW")
        assert loaded is not None
        assert loaded.form_type == "8-K"
        body = body_store.get(loaded.body_s3_key)
        assert body is not None
        assert "new body" in body


def test_process_ticker_skips_already_indexed() -> None:
    """Already-indexed filings are not re-fetched — rate-limit
    discipline + cost control."""

    import time

    with mock_aws():
        table = _table()
        _bucket()
        s3 = boto3.client("s3", region_name="us-west-2")
        idx = FilingsIndexStore(table)
        body_store = FilingsBodyStore(s3_client=s3, bucket="ts-filings-test")

        # Pre-populate with accession "A".
        idx.put(FilingIndex(
            ticker="AAPL", accession="A", form_type="8-K",
            filing_date="2026-04-27", summary="",
            body_s3_key="k", indexed_at=int(time.time()),
        ))
        edgar = _StubEdgar(
            submissions=_submissions_shape(
                form_types=["8-K"], accessions=["A"],
            ),
        )
        n = process_ticker(
            ticker="AAPL", cik="0000320193",
            client=edgar,
            index_store=idx, body_store=body_store,
            form_types=None,
        )
        assert n == 0
        assert edgar.body_fetches == []


def test_process_ticker_body_fetch_failure_skips_filing() -> None:
    """One flaky filing doesn't break the whole run — watcher logs
    and moves on. Subsequent invocations will retry."""

    with mock_aws():
        table = _table()
        _bucket()
        s3 = boto3.client("s3", region_name="us-west-2")
        idx = FilingsIndexStore(table)
        body_store = FilingsBodyStore(s3_client=s3, bucket="ts-filings-test")

        class _FlakyEdgar(_StubEdgar):
            def filing_body(self, cik: str, accession: str, primary_doc: str) -> str:
                raise RuntimeError("edgar 503")

        edgar = _FlakyEdgar(
            submissions=_submissions_shape(
                form_types=["8-K"], accessions=["A-FLAKY"],
            ),
        )
        n = process_ticker(
            ticker="AAPL", cik="0000320193",
            client=edgar,
            index_store=idx, body_store=body_store,
            form_types=None,
        )
        assert n == 0
        # Nothing landed in the index for the flaky filing.
        assert idx.get("AAPL", "A-FLAKY") is None
