"""EDGAR watcher.

Runs every 15 min during market hours. For each ticker in the union
of active strategies' watched symbols:

  1. Resolve ticker → CIK via a cached map of SEC's company_tickers.json
  2. Fetch the company's submissions JSON (recent filings metadata)
  3. For each recent filing of interest NOT already in our DDB index:
       - fetch the primary document body
       - upload to S3
       - write the DDB index row

Pure functions (parse_submissions_json, process_ticker) are the
test surface; EdgarClient is a thin HTTP shim over urllib, stubbed
in tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

import structlog

from trading_strands.filings_store.stores import (
    FilingIndex,
    FilingsBodyStore,
    FilingsIndexStore,
    build_body_key,
)
from trading_strands.strategies_store.store import (
    StrategyStatus,
    StrategyStore,
)

logger = structlog.get_logger()
logging.getLogger("botocore").setLevel(logging.WARNING)

# Default forms watched when strategies don't specify. 8-K =
# material events, Form 4 = insider transactions. Broader set
# (10-K, 10-Q) is available but noisier; opt in per strategy later.
_DEFAULT_FORM_TYPES = ("8-K", "4")

# How many recent filings per ticker to consider each run. SEC's
# submissions endpoint returns up to ~1000 recent; 20 is plenty
# given a 15-min cadence.
_MAX_PER_TICKER = 20

# Rate-limit buffer between EDGAR calls (seconds). 10 req/s is
# SEC's stated limit; we pace well below to share with other
# consumers from the same IP.
_FETCH_DELAY_SECONDS = 0.15


class EdgarClient:
    """Thin HTTP client for SEC EDGAR endpoints.

    Required: a User-Agent identifying TradingStrands. SEC's
    guidance is for operators to include a contact email; the
    watcher's env supplies that.
    """

    COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik}.json"
    # Filing body base: https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/{accession_no_dashes}/{primary_doc}
    BODY_URL_TMPL = (
        "https://www.sec.gov/Archives/edgar/data/{cik_no_pad}/"
        "{accession_no_dashes}/{primary_doc}"
    )

    def __init__(self, user_agent: str) -> None:
        if not user_agent:
            msg = "EdgarClient requires a User-Agent (SEC requirement)"
            raise ValueError(msg)
        self._ua = user_agent

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(  # noqa: S310 — https only by construction
            url,
            headers={"User-Agent": self._ua, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return bytes(resp.read())

    def company_tickers(self) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(
            self._get(self.COMPANY_TICKERS_URL).decode("utf-8"),
        )
        return parsed

    def submissions(self, cik: str) -> dict[str, Any]:
        url = self.SUBMISSIONS_URL_TMPL.format(cik=cik)
        parsed: dict[str, Any] = json.loads(
            self._get(url).decode("utf-8"),
        )
        return parsed

    def filing_body(
        self, cik: str, accession: str, primary_doc: str,
    ) -> str:
        """Fetch the primary HTML body for a filing. Returns the
        decoded string; callers decide whether to truncate."""

        accession_no_dashes = accession.replace("-", "")
        cik_no_pad = cik.lstrip("0") or "0"
        url = self.BODY_URL_TMPL.format(
            cik_no_pad=cik_no_pad,
            accession_no_dashes=accession_no_dashes,
            primary_doc=primary_doc,
        )
        body = self._get(url)
        return body.decode("utf-8", errors="replace")


def build_ticker_to_cik_map(
    raw: dict[str, Any],
) -> dict[str, str]:
    """Flatten SEC's company_tickers.json into TICKER → zero-padded
    10-digit CIK string."""

    out: dict[str, str] = {}
    for _key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).upper()
        cik_raw = entry.get("cik_str")
        if not ticker or cik_raw is None:
            continue
        out[ticker] = str(int(cik_raw)).zfill(10)
    return out


def parse_submissions_json(
    raw: dict[str, Any],
    ticker: str,
    form_types: list[str] | None,
    max_count: int,
) -> list[dict[str, Any]]:
    """Extract filings from SEC's submissions JSON into a simple
    list of dicts keyed by our internal field names. Filters on
    form_types when specified; stops at max_count."""

    recent = (
        raw.get("filings", {}).get("recent", {})
        if isinstance(raw, dict) else {}
    )
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primaries = recent.get("primaryDocument") or []

    out: list[dict[str, Any]] = []
    # The SEC feed is newest-first, so iterate in order.
    for i in range(min(len(forms), len(accessions), len(dates), len(primaries))):
        form = str(forms[i])
        if form_types is not None and form not in form_types:
            continue
        out.append({
            "ticker": ticker.upper(),
            "accession": str(accessions[i]),
            "form_type": form,
            "filing_date": str(dates[i]),
            "primary_document": str(primaries[i]),
        })
        if len(out) >= max_count:
            break
    return out


def process_ticker(
    ticker: str,
    cik: str,
    client: Any,
    index_store: FilingsIndexStore,
    body_store: FilingsBodyStore,
    form_types: list[str] | None,
) -> int:
    """Pull any new filings for the ticker and write them to the
    caches. Returns the count landed this run.

    Failures on individual filings don't abort the whole ticker —
    log and continue. The next invocation will retry.
    """

    try:
        raw = client.submissions(cik)
    except Exception:
        logger.exception("edgar.submissions_failed", ticker=ticker, cik=cik)
        return 0

    parsed = parse_submissions_json(
        raw, ticker=ticker,
        form_types=form_types or list(_DEFAULT_FORM_TYPES),
        max_count=_MAX_PER_TICKER,
    )

    landed = 0
    for filing in parsed:
        accession = filing["accession"]
        if index_store.get(ticker, accession) is not None:
            # Already cached — respect rate limits, skip.
            continue
        try:
            body = client.filing_body(
                cik=cik,
                accession=accession,
                primary_doc=filing["primary_document"],
            )
        except Exception:
            logger.exception(
                "edgar.body_fetch_failed",
                ticker=ticker, accession=accession,
            )
            continue

        s3_key = build_body_key(ticker, filing["form_type"], accession)
        try:
            body_store.put(s3_key, body)
        except Exception:
            logger.exception(
                "edgar.body_put_failed",
                ticker=ticker, accession=accession,
            )
            continue

        summary = (body[:500].strip() if body else "")
        index_store.put(FilingIndex(
            ticker=ticker.upper(),
            accession=accession,
            form_type=filing["form_type"],
            filing_date=filing["filing_date"],
            summary=summary,
            body_s3_key=s3_key,
            indexed_at=int(time.time()),
        ))
        landed += 1
        # Pace between writes to stay well below SEC's rate ceiling.
        time.sleep(_FETCH_DELAY_SECONDS)
    return landed


def _watched_symbols(store: StrategyStore) -> set[str]:
    """Union of ACTIVE strategies' symbol sets. Uppercased."""

    out: set[str] = set()
    for s in store.list_all():
        if s.status != StrategyStatus.ACTIVE:
            continue
        for sym in s.symbols:
            if sym:
                out.add(sym.upper())
    return out


def _run(
    table: Any,
    s3_client: Any,
    bucket: str,
    user_agent: str,
) -> dict[str, Any]:
    """Main body. Tests can construct this directly."""

    strategy_store = StrategyStore(table)
    index_store = FilingsIndexStore(table)
    body_store = FilingsBodyStore(s3_client=s3_client, bucket=bucket)
    client = EdgarClient(user_agent=user_agent)

    symbols = _watched_symbols(strategy_store)
    if not symbols:
        logger.info("edgar.no_symbols_to_watch")
        return {"ok": True, "tickers": 0, "landed": 0}

    try:
        ticker_map_raw = client.company_tickers()
    except Exception:
        logger.exception("edgar.ticker_map_failed")
        return {"ok": False, "error": "ticker_map_failed"}
    ticker_to_cik = build_ticker_to_cik_map(ticker_map_raw)

    total_landed = 0
    unknown_tickers: list[str] = []
    for ticker in sorted(symbols):
        cik = ticker_to_cik.get(ticker)
        if cik is None:
            unknown_tickers.append(ticker)
            continue
        landed = process_ticker(
            ticker=ticker,
            cik=cik,
            client=client,
            index_store=index_store,
            body_store=body_store,
            form_types=None,
        )
        total_landed += landed

    logger.info(
        "edgar.run_complete",
        tickers=len(symbols),
        unknown=len(unknown_tickers),
        landed=total_landed,
    )
    return {
        "ok": True,
        "tickers": len(symbols),
        "unknown_tickers": unknown_tickers,
        "landed": total_landed,
    }


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point. Env:
        DYNAMODB_TABLE
        FILINGS_BUCKET
        EDGAR_USER_AGENT  — e.g., "TradingStrands ops@example.com"
    """

    import boto3

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(os.environ["DYNAMODB_TABLE"])
    s3 = boto3.client("s3")
    bucket = os.environ["FILINGS_BUCKET"]
    ua = os.environ.get("EDGAR_USER_AGENT", "")
    return _run(table=table, s3_client=s3, bucket=bucket, user_agent=ua)
