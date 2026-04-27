"""TA snapshot computer — scheduled Lambda.

Runs every 5 min during market hours. Walks the union of active
strategies' watched symbols, pulls recent minute bars from
MarketDataStore, computes the indicator bundle, writes the snapshot
to TASnapshotStore. Strategy bots read the freshest snapshot at
decision time (no HTTP round-trip; DDB only).
"""

from trading_strands.ta_computer.computer import (
    closes_from_hour_map,
    collect_watched_symbols,
    handler,
    run_compute_for_symbol,
)

__all__ = [
    "closes_from_hour_map",
    "collect_watched_symbols",
    "handler",
    "run_compute_for_symbol",
]
