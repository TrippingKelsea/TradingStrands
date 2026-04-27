"""EDGAR watcher — scheduled Lambda that populates the filings cache.

SEC EDGAR rate-limits hard (10 req/sec per IP, User-Agent required).
Strategy tools never hit EDGAR directly; this watcher is the sole
writer. It runs every 15 min during market hours, walks the union
of tickers any active strategy watches, and pulls any filings that
aren't yet in the cache.
"""

from trading_strands.edgar_watcher.watcher import (
    EdgarClient,
    build_ticker_to_cik_map,
    handler,
    parse_submissions_json,
    process_ticker,
)

__all__ = [
    "EdgarClient",
    "build_ticker_to_cik_map",
    "handler",
    "parse_submissions_json",
    "process_ticker",
]
