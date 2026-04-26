"""Market data island — the shared store of recorded market data.

Every active strategy generates market-data observations as it runs. Rather
than each strategy privately caching, we write those observations to a
shared DynamoDB store so:

  - Multiple strategies watching the same symbol share one copy of the data
  - The Self-Critique Agent can read the exact data a strategy saw last week
  - The chat feature can answer "what did the market look like at 10:32?"
  - The Auditor can reconstruct any decision's inputs from durable storage

Schema:
    MARKETDATA#{symbol}#{yyyymmddhh}  (pk)
      minute_bars: Map<"mm:ss" -> {open, high, low, close, volume, price}>
      last_updated: int (unix ts of most recent write)
      ttl: int (90 days from first write, DDB TTL column)

Partition key encodes (symbol, hour) to keep items bounded in size while
still cheap to read for "the past N minutes." A single hour's item holds up
to 60 minute-bars.

90-day rolling retention is the default, configurable via
`MARKETDATA_RETENTION_DAYS` env var at the system-org level (enforced by
bootstrap, not by this module — this module just honors whatever retention
window the writer specifies).

See docs/SPEC/agent_memory.md for how agents reference MARKETDATA items
from their memory files, and docs/SPEC/agents.md for why the island is
shared across orgs (it's platform infrastructure, not per-org accounting).
"""

from trading_strands.marketdata_store.store import (
    MarketBar,
    MarketDataStore,
    hour_bucket,
)

__all__ = ["MarketBar", "MarketDataStore", "hour_bucket"]
