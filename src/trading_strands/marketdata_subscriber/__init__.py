"""Market Data Subscriber service.

Runs 24/7 in its own Fargate task. Pulls quotes for the union of
symbols any active strategy watches and writes them to MarketDataStore.
The trading service still fetches its own prices during market hours;
the subscriber runs alongside and populates the shared store so that
premarket + extended-hours data is available regardless of whether
any strategy task is running.

v1: subscriber takes over *all* price fetches and strategy tasks read
from MarketDataStore instead of calling the broker. That cutover is
deliberately separate — this module only writes in parallel.
"""

from trading_strands.marketdata_subscriber.loop import (
    FetchError,
    PollSummary,
    poll_once,
    run_forever,
    watched_symbols,
)

__all__ = [
    "FetchError",
    "PollSummary",
    "poll_once",
    "run_forever",
    "watched_symbols",
]
