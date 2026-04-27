"""TA snapshot storage + summary formatter.

One row per (symbol, date, hour):
    pk = TA_SNAPSHOT#{symbol}#{yyyy-mm-dd}#{hh}

Scheduled computer Lambda writes these at a cadence (5 min during
market hours by default). Strategy bots read the most-recent snapshot
for each of their symbols at decision time, render it into the prompt.

Write-whole-row semantics: re-running the computer for the same hour
overwrites. The hour bucket is fine-grained enough that stale data
ages out on its own within 60 minutes; explicit TTL on the item
purges anything older than ~30 days (keeps the table small for the
post-mortem window).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

_SNAPSHOT_TTL_SECONDS = 30 * 24 * 3600
PK_PREFIX = "TA_SNAPSHOT#"


class TASnapshot(BaseModel):
    """One symbol's indicators at one point in time. Any indicator can
    be None when there aren't enough bars to compute it (short data
    history for a new symbol, very short window)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    computed_at: int   # unix seconds, UTC
    last_close: Decimal | None = None
    rsi_14: Decimal | None = None
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_hist: Decimal | None = None
    sma_20: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    bb_upper: Decimal | None = None
    bb_middle: Decimal | None = None
    bb_lower: Decimal | None = None


class TASnapshotStore:
    """DDB-backed read/write of per-symbol TA snapshots."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def put_snapshot(self, snap: TASnapshot) -> None:
        """Write the snapshot. Key uses the snapshot's computed_at
        to derive the hour bucket, so callers don't have to."""

        lt = time.gmtime(snap.computed_at)
        date = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        hour = f"{lt.tm_hour:02d}"
        # DDB's JSON marshalling is picky about Decimal precision;
        # round-trip through Pydantic's JSON mode to preserve the
        # None-vs-value distinction.
        self._table.put_item(Item={
            "pk": f"{PK_PREFIX}{snap.symbol}#{date}#{hour}",
            "symbol": snap.symbol,
            "date": date,
            "hour": hour,
            "payload_json": snap.model_dump_json(),
            "computed_at": snap.computed_at,
            "ttl": snap.computed_at + _SNAPSHOT_TTL_SECONDS,
        })

    def get_latest(
        self, symbol: str, lookback_hours: int = 6,
    ) -> TASnapshot | None:
        """Walk back up to `lookback_hours` from now to find the most
        recent snapshot for the symbol. Returns None if nothing in
        the window.

        Lookback default of 6 hours is enough for a strategy that
        boots after a market-open gap — the computer Lambda writes
        every 5 min during market hours, so anything within 6h is
        fresh enough to be useful, and premarket/post-close gaps
        don't erase the morning snapshot.
        """

        now = int(time.time())
        for offset in range(lookback_hours + 1):
            ts = now - offset * 3600
            lt = time.gmtime(ts)
            date = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            hour = f"{lt.tm_hour:02d}"
            resp = self._table.get_item(
                Key={"pk": f"{PK_PREFIX}{symbol}#{date}#{hour}"},
            )
            item = resp.get("Item")
            if item is None:
                continue
            return TASnapshot.model_validate_json(
                str(item["payload_json"]),
            )
        return None


# ── Summary formatter ──────────────────────────────────────────────


def _fmt_dec(v: Decimal | None, places: int = 2) -> str:
    """Render a Decimal indicator, handling None as a dash."""

    if v is None:
        return "—"
    quant = Decimal("1").scaleb(-places)
    return str(v.quantize(quant))


def _fmt_bb_position(
    last: Decimal | None,
    upper: Decimal | None,
    middle: Decimal | None,
    lower: Decimal | None,
) -> str:
    """Describe where the last close sits inside the Bollinger bands.

    Operators and the LLM both read this more easily than raw band
    numbers — 'upper third' beats '152.34 (bands 150.02 / 151.18 /
    152.34)' for quick reasoning. Both appear; this is the summary.
    """

    if last is None or upper is None or middle is None or lower is None:
        return "—"
    if last >= upper:
        return "above upper band"
    if last <= lower:
        return "below lower band"
    # Within bands: classify into upper-third / middle / lower-third.
    width = upper - lower
    if width <= Decimal("0"):   # flat series
        return "at middle (flat)"
    third = width / Decimal("3")
    if last >= lower + Decimal("2") * third:
        return "upper third"
    if last >= lower + third:
        return "middle third"
    return "lower third"


def _render_symbol_block(snap: TASnapshot) -> str:
    bb_pos = _fmt_bb_position(
        snap.last_close, snap.bb_upper, snap.bb_middle, snap.bb_lower,
    )
    # One line per symbol keeps the total prompt cost bounded even
    # for strategies that watch many tickers. Order of fields matches
    # what a trader's eye scans first: price, RSI, MACD direction,
    # trend MAs, BB band position.
    return (
        f"{snap.symbol}: "
        f"close {_fmt_dec(snap.last_close)} | "
        f"RSI14 {_fmt_dec(snap.rsi_14, 1)} | "
        f"MACD {_fmt_dec(snap.macd, 3)}/{_fmt_dec(snap.macd_signal, 3)} "
        f"(h {_fmt_dec(snap.macd_hist, 3)}) | "
        f"SMA 20/50/200: {_fmt_dec(snap.sma_20)}/"
        f"{_fmt_dec(snap.sma_50)}/{_fmt_dec(snap.sma_200)} | "
        f"BB: {bb_pos}"
    )


def summarize_for_symbols(
    symbols: set[str],
    store: TASnapshotStore,
) -> str:
    """Produce the TA block the decision prompt injects.

    - No symbols → concise placeholder (strategies with dynamic
      symbol selection don't get TA; they fetch symbols at decide
      time and by then it's too late to inject).
    - For each symbol: walk up to 6h back for the latest snapshot;
      render one line. Missing snapshots render as "(no data)".
    """

    if not symbols:
        return "(no symbols — TA not applicable)"

    lines: list[str] = []
    for sym in sorted(symbols):
        snap = store.get_latest(sym)
        if snap is None:
            lines.append(f"{sym}: (no recent TA snapshot)")
        else:
            lines.append(_render_symbol_block(snap))
    return "\n".join(lines)
