"""Calendar fetcher Lambda.

Daily scheduled Lambda that hits Finnhub's earnings + economic
calendar endpoints and writes the results to CalendarStore. Uses a
platform-level API key at trading-strands/calendar per SPEC/tools.md
§5.5 — calendar data is global so one fetch covers every org.
"""

from trading_strands.calendar_fetcher.fetcher import (
    FinnhubClient,
    build_calendar_day,
    fetch_and_store_day,
    handler,
)

__all__ = [
    "FinnhubClient",
    "build_calendar_day",
    "fetch_and_store_day",
    "handler",
]
