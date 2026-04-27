"""Economic + earnings calendar store.

See docs/SPEC/tools.md §3.1 for the delivery model (context injection,
not a tool call). A scheduled fetcher Lambda (separate commit) writes
CALENDAR#{date} rows; strategy bots read them at decision time and
include a short summary in the LLM prompt.
"""

from trading_strands.calendar_store.store import (
    CalendarDay,
    CalendarStore,
    EarningsEvent,
    EconomicEvent,
    summarize_for_symbols,
)

__all__ = [
    "CalendarDay",
    "CalendarStore",
    "EarningsEvent",
    "EconomicEvent",
    "summarize_for_symbols",
]
