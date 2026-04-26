"""DynamoDB-backed market data store.

The current writer is the existing trading service — on each tick, the
orchestrator hands observed prices to MarketDataStore.record_tick() which
aggregates into minute-bar buckets keyed by (symbol, hour).

In v1, the Market Data Subscriber Agent (docs/SPEC/agents.md) takes over
writes from a dedicated service subscribing to Alpaca's WebSocket feed.
The schema below is forward-compatible with that — the subscriber produces
the same MARKETDATA# items, just at higher frequency + broader coverage.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, NamedTuple

DEFAULT_RETENTION_DAYS = 90


def hour_bucket(ts: float) -> str:
    """Return the YYYYMMDDHH key for a given unix timestamp (UTC).

    All bucketing is UTC regardless of the market's local timezone. The
    dashboard converts to user-preferred TZ at display time. This keeps
    the DB layer timezone-agnostic — rollover at midnight UTC is the only
    boundary the store cares about.
    """

    lt = time.gmtime(ts)
    return f"{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}{lt.tm_hour:02d}"


def minute_second_key(ts: float) -> str:
    """Return 'MM:SS' key within a minute-bar map."""

    lt = time.gmtime(ts)
    return f"{lt.tm_min:02d}:{lt.tm_sec:02d}"


class MarketBar(NamedTuple):
    """A single observation within a minute-bar bucket.

    Current writer (orchestrator tick) only has `price` — open/high/low/close
    are synthesized from the price stream when a bar completes. The v1
    WebSocket subscriber will write all fields directly.
    """

    timestamp: int  # unix seconds, exact moment of observation
    symbol: str
    price: Decimal
    volume: Decimal = Decimal(0)


class MarketDataStore:
    """Write + read minute-bar aggregates.

    Stateless with respect to DDB — pass a boto3 Table handle at construct
    time. The store maintains a small in-memory write buffer (current
    minute's observations) before committing to DDB on minute rollover,
    so we don't pay a write per tick. Flushed explicitly on shutdown.
    """

    def __init__(
        self, table: Any, retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._table = table
        self._retention_days = retention_days
        # Buffer: (symbol, yyyymmddhh, mm) -> list of MarketBar
        # Flushed when the minute rolls over, or when explicitly flushed.
        self._buffer: dict[tuple[str, str, str], list[MarketBar]] = {}

    def record_tick(self, bar: MarketBar) -> None:
        """Record an observation. Buffers internally; writes on minute
        rollover or explicit flush.

        Cheap enough to call on every tick — just a dict append until the
        minute boundary arrives.
        """

        bucket = hour_bucket(bar.timestamp)
        lt = time.gmtime(bar.timestamp)
        minute = f"{lt.tm_min:02d}"
        key = (bar.symbol, bucket, minute)
        self._buffer.setdefault(key, []).append(bar)

        # If there are any buffered observations for DIFFERENT minutes on
        # this symbol, those minutes are complete — flush them.
        self._flush_completed_minutes(bar.symbol, bucket, minute)

    def _flush_completed_minutes(
        self, current_symbol: str, current_bucket: str, current_minute: str,
    ) -> None:
        """Write any minute buffers that aren't the currently-forming one
        for this symbol. The current minute is still being populated, so
        keep it in memory.
        """

        completed: list[tuple[str, str, str]] = []
        for key in self._buffer:
            sym, bucket, minute = key
            if sym == current_symbol and bucket == current_bucket and minute == current_minute:
                continue
            if sym == current_symbol:
                completed.append(key)

        for key in completed:
            self._flush_one(key)

    def flush(self) -> None:
        """Write all buffered minutes to DDB. Called on shutdown or when
        the caller wants durability guarantees immediately."""

        keys = list(self._buffer.keys())
        for key in keys:
            self._flush_one(key)

    def _flush_one(self, key: tuple[str, str, str]) -> None:
        """Aggregate one minute's observations into a bar and UPDATE the
        bucket item in DDB.

        Uses UpdateItem with SET on a nested map key so concurrent writers
        to different minutes of the same hour don't clobber each other.
        (In v0 there is only one writer; we keep this defensive anyway to
        simplify the v1 migration to a dedicated subscriber.)
        """

        observations = self._buffer.pop(key, [])
        if not observations:
            return

        symbol, bucket, minute = key
        prices = [o.price for o in observations]
        total_volume = sum((o.volume for o in observations), Decimal(0))
        bar = {
            "open": str(prices[0]),
            "high": str(max(prices)),
            "low": str(min(prices)),
            "close": str(prices[-1]),
            "volume": str(total_volume),
            "samples": len(observations),
            "first_ts": observations[0].timestamp,
            "last_ts": observations[-1].timestamp,
        }

        pk = f"MARKETDATA#{symbol}#{bucket}"
        ttl = int(time.time()) + self._retention_days * 86400

        # Two-step: ensure the item + minute_bars map exist, THEN set the
        # minute's bar. Cannot do both in one UpdateExpression because you
        # can't SET a nested map key in the same expression that initializes
        # the containing map.
        self._table.update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "SET minute_bars = if_not_exists(minute_bars, :empty), "
                "#ttl = if_not_exists(#ttl, :ttl), "
                "symbol = if_not_exists(symbol, :sym), "
                "#bucket = if_not_exists(#bucket, :bucket)"
            ),
            # 'bucket' is a DDB reserved word; 'ttl' technically isn't
            # but we alias both for consistency.
            ExpressionAttributeNames={"#ttl": "ttl", "#bucket": "bucket"},
            ExpressionAttributeValues={
                ":empty": {},
                ":ttl": ttl,
                ":sym": symbol,
                ":bucket": bucket,
            },
        )
        self._table.update_item(
            Key={"pk": pk},
            UpdateExpression="SET minute_bars.#m = :bar, last_updated = :ts",
            ExpressionAttributeNames={"#m": minute},
            ExpressionAttributeValues={
                ":bar": bar,
                ":ts": int(time.time()),
            },
        )

    def get_hour(self, symbol: str, bucket: str) -> dict[str, dict[str, Any]]:
        """Read all minute-bars for (symbol, hour). Returns empty dict if
        no data for that hour. Key of the returned dict is 'MM'."""

        resp = self._table.get_item(Key={"pk": f"MARKETDATA#{symbol}#{bucket}"})
        item = resp.get("Item") or {}
        return dict(item.get("minute_bars", {}))

    def get_range(
        self, symbol: str, start_ts: float, end_ts: float,
    ) -> list[tuple[int, dict[str, Any]]]:
        """Read all bars from `symbol` between two unix timestamps,
        returned as [(ts, bar), ...] sorted ascending.

        Spans as many hour-buckets as needed; reads each bucket with a
        single GetItem. N reads for an N-hour window.
        """

        out: list[tuple[int, dict[str, Any]]] = []
        # Round start down to hour, end up to hour, iterate.
        h = int(start_ts // 3600) * 3600
        while h <= end_ts:
            bucket = hour_bucket(h)
            minutes = self.get_hour(symbol, bucket)
            for _minute_str, bar in minutes.items():
                last_ts = int(bar.get("last_ts", 0))
                if start_ts <= last_ts <= end_ts:
                    out.append((last_ts, dict(bar)))
            h += 3600
        out.sort(key=lambda x: x[0])
        return out
