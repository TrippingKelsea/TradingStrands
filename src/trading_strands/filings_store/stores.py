"""Filings storage: DDB index + S3 body cache.

Layout:
    DDB: FILING_INDEX#{ticker}#{accession}
         { ticker, accession, form_type, filing_date (YYYY-MM-DD),
           summary, body_s3_key, indexed_at, ttl }
    S3:  {ticker}/{form_type}/{accession}.html   body raw

Ticker is always uppercased at store boundaries so the reader and
watcher see the same keys regardless of how a strategy typed its
symbols. Accession is SEC's canonical identifier — natural dedup.

The spec (§5.5 in current doc, §5.6 after rename) specifies Glacier
IR at 30d and delete at 90d via S3 lifecycle — the bucket itself
enforces that; this module writes unconditionally.
"""

from __future__ import annotations

import time
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict

from trading_strands.ddb import scan_all

PK_PREFIX = "FILING_INDEX#"

# Mirror S3 90d lifecycle — a row whose S3 body was Glacier-deleted
# shouldn't keep dangling in the index. TTL of the DDB row ensures
# cleanup even if S3 and DDB drift.
_INDEX_TTL_SECONDS = 90 * 24 * 3600


class FilingIndex(BaseModel):
    """DDB index row for one filing."""

    model_config = ConfigDict(extra="ignore")

    ticker: str
    accession: str
    form_type: str
    filing_date: str          # YYYY-MM-DD
    summary: str
    body_s3_key: str
    indexed_at: int


def build_body_key(
    ticker: str, form_type: str, accession: str,
) -> str:
    """Canonical S3 key. Ticker uppercased for consistency."""

    return f"{ticker.upper()}/{form_type}/{accession}.html"


def _pk(ticker: str, accession: str) -> str:
    return f"{PK_PREFIX}{ticker.upper()}#{accession}"


class FilingsIndexStore:
    """DDB index for fast per-ticker queries."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def put(self, filing: FilingIndex) -> None:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": _pk(filing.ticker, filing.accession),
            "ticker": filing.ticker.upper(),
            "accession": filing.accession,
            "form_type": filing.form_type,
            "filing_date": filing.filing_date,
            "summary": filing.summary,
            "body_s3_key": filing.body_s3_key,
            "indexed_at": filing.indexed_at,
            "ttl": now + _INDEX_TTL_SECONDS,
        })

    def get(
        self, ticker: str, accession: str,
    ) -> FilingIndex | None:
        resp = self._table.get_item(Key={"pk": _pk(ticker, accession)})
        item = resp.get("Item")
        if item is None:
            return None
        return FilingIndex(
            ticker=str(item["ticker"]),
            accession=str(item["accession"]),
            form_type=str(item["form_type"]),
            filing_date=str(item["filing_date"]),
            summary=str(item.get("summary", "")),
            body_s3_key=str(item["body_s3_key"]),
            indexed_at=int(item["indexed_at"]),
        )

    def list_for_ticker(
        self,
        ticker: str,
        form_types: list[str] | None,
        days_back: int,
    ) -> list[FilingIndex]:
        """Recent filings for a ticker, newest first. Filters on
        form type (if given) and filing_date within days_back."""

        prefix = f"{PK_PREFIX}{ticker.upper()}#"
        items = scan_all(self._table, Attr("pk").begins_with(prefix))
        # Filter by date window on filing_date (YYYY-MM-DD sortable).
        cutoff_epoch = int(time.time()) - days_back * 86400
        cutoff_lt = time.gmtime(cutoff_epoch)
        cutoff_date = (
            f"{cutoff_lt.tm_year:04d}-{cutoff_lt.tm_mon:02d}-"
            f"{cutoff_lt.tm_mday:02d}"
        )
        filtered: list[FilingIndex] = []
        for item in items:
            if str(item.get("filing_date", "")) < cutoff_date:
                continue
            form = str(item.get("form_type", ""))
            if form_types is not None and form not in form_types:
                continue
            filtered.append(FilingIndex(
                ticker=str(item["ticker"]),
                accession=str(item["accession"]),
                form_type=form,
                filing_date=str(item["filing_date"]),
                summary=str(item.get("summary", "")),
                body_s3_key=str(item["body_s3_key"]),
                indexed_at=int(item["indexed_at"]),
            ))
        # Newest first by filing_date, accession as tiebreaker.
        filtered.sort(
            key=lambda f: (f.filing_date, f.accession),
            reverse=True,
        )
        return filtered


class FilingsBodyStore:
    """S3-backed filing body cache. Reader-only in the tool path;
    only the watcher writes."""

    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def put(self, key: str, body: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket, Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )

    def get(
        self, key: str, max_bytes: int | None = None,
    ) -> str | None:
        """Fetch the body, optionally truncating to max_bytes.

        Truncation happens at the store layer because the tool path
        wants a consistent "max size" contract regardless of how it
        was called. An LLM doesn't need 500 KB of EDGAR XBRL.
        """

        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        except self._s3.exceptions.NoSuchKey:
            return None
        except self._s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        body: bytes = resp["Body"].read()
        text = body.decode("utf-8", errors="replace")
        if max_bytes is not None and len(text) > max_bytes:
            return text[:max_bytes]
        return text
