"""Calendar DDB storage + summary formatter.

One row per calendar date:

    pk = CALENDAR#{yyyy-mm-dd}
    economic = [<EconomicEvent JSON>, ...]   global macro events
    earnings = {symbol: [<EarningsEvent JSON>, ...]}   per-symbol
    updated_at = <epoch>
    ttl = <epoch>   # expire ~30 days after the date, keeps table small

Chose one row per day (not per-symbol-per-day) for two reasons:
  1. One GetItem per tick serves the whole day's context — bots don't
     multiply reads as they add symbols.
  2. Earnings + economic payload for a full US market day stays well
     under DDB's 400 KB limit (S&P 500 earnings plus BEA/BLS releases
     is tens of KB at most).

If we ever need per-symbol pagination (e.g., international market
expansion), splitting to CALENDAR_EARNINGS#{symbol}#{date} is a
straightforward migration.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Keep the table lean: a calendar row is only interesting for a day
# or two either side of the date. 30d retention gives operators a
# window for post-mortem ("was there earnings on the day X blew up?").
_CALENDAR_TTL_SECONDS = 30 * 24 * 3600

PK_PREFIX = "CALENDAR#"


class EconomicEvent(BaseModel):
    """A macro economic release or scheduled event affecting all
    symbols — FOMC, CPI, NFP, Fed speeches, Treasury auctions."""

    model_config = ConfigDict(extra="ignore")

    time_utc: str   # "HH:MM" 24h UTC
    title: str
    importance: str = "medium"  # low | medium | high
    country: str = "US"


class EarningsEvent(BaseModel):
    """A per-symbol earnings release."""

    model_config = ConfigDict(extra="ignore")

    # "before_market", "after_market", or "HH:MM" for pre/post halt events.
    time_of_day: str
    eps_estimate: str | None = None
    revenue_estimate: str | None = None


class CalendarDay(BaseModel):
    """Full calendar payload for one date."""

    model_config = ConfigDict(extra="ignore")

    date: str   # yyyy-mm-dd (UTC boundary)
    economic: list[EconomicEvent] = Field(default_factory=list)
    earnings: dict[str, list[EarningsEvent]] = Field(default_factory=dict)


class CalendarStore:
    """DDB-backed read/write of CalendarDay rows."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def put_day(self, day: CalendarDay) -> None:
        """Write-whole-row semantics. Re-running the fetcher for the
        same date overwrites cleanly."""

        self._table.put_item(Item={
            "pk": f"{PK_PREFIX}{day.date}",
            "date": day.date,
            "economic": [e.model_dump(mode="json") for e in day.economic],
            # DDB rejects empty maps; encode as JSON string if empty.
            "earnings_json": json.dumps({
                sym: [e.model_dump(mode="json") for e in events]
                for sym, events in day.earnings.items()
            }),
            "updated_at": int(time.time()),
            "ttl": int(time.time()) + _CALENDAR_TTL_SECONDS,
        })

    def get_day(self, date: str) -> CalendarDay | None:
        resp = self._table.get_item(Key={"pk": f"{PK_PREFIX}{date}"})
        item = resp.get("Item")
        if item is None:
            return None
        econ_raw = item.get("economic", []) or []
        earnings_raw_json = str(item.get("earnings_json") or "{}")
        earnings_raw = json.loads(earnings_raw_json)
        return CalendarDay(
            date=str(item["date"]),
            economic=[EconomicEvent.model_validate(e) for e in econ_raw],
            earnings={
                sym: [EarningsEvent.model_validate(e) for e in events]
                for sym, events in earnings_raw.items()
            },
        )


# ── Summary formatter ──────────────────────────────────────────────


def _format_event_line(event: EconomicEvent) -> str:
    importance_tag = {
        "high": " [high]",
        "medium": "",
        "low": " [low]",
    }.get(event.importance, "")
    return f"  - {event.time_utc} UTC: {event.title}{importance_tag}"


def _format_earnings_line(symbol: str, e: EarningsEvent) -> str:
    when = {
        "before_market": "BMO",
        "after_market": "AMC",
    }.get(e.time_of_day, e.time_of_day)
    bits = [f"{symbol} {when}"]
    if e.eps_estimate:
        bits.append(f"EPS est {e.eps_estimate}")
    if e.revenue_estimate:
        bits.append(f"rev est {e.revenue_estimate}")
    return "  - " + " · ".join(bits)


def _day_block(
    label: str,
    symbols: set[str],
    day: CalendarDay,
) -> list[str]:
    """Render one day's events as markdown lines, filtered to
    `symbols` for the earnings part. Macro events always included."""

    lines: list[str] = [f"{label} ({day.date})"]
    if day.economic:
        for ev in day.economic:
            lines.append(_format_event_line(ev))
    relevant_earnings = [
        (sym, e)
        for sym in sorted(symbols & day.earnings.keys())
        for e in day.earnings.get(sym, [])
    ]
    if relevant_earnings:
        for sym, e in relevant_earnings:
            lines.append(_format_earnings_line(sym, e))
    if len(lines) == 1:   # just the label, nothing under it
        lines.append("  (no relevant events)")
    return lines


def summarize_for_symbols(
    symbols: set[str],
    today: CalendarDay | None,
    tomorrow: CalendarDay | None,
) -> str:
    """Produce the prompt block the strategy decision context includes.

    Format is plain markdown with a `Today` / `Tomorrow` section.
    Macro events appear regardless of the strategy's symbols; earnings
    are filtered to the subscribed set. If both days are None the
    block simply says calendar data is unavailable — distinct from
    'fetched and nothing interesting', which says 'no relevant events'.
    """

    if today is None and tomorrow is None:
        return "Calendar data unavailable."

    parts: list[str] = []
    # Case: today fetched but produced no relevant events AND no
    # macro events AND no lookahead — keep it terse (covered by the
    # empty-day test).
    if today is not None:
        parts.extend(_day_block("Today", symbols, today))
    if tomorrow is not None:
        if parts:
            parts.append("")
        parts.extend(_day_block("Tomorrow", symbols, tomorrow))

    # Collapse to "no relevant events" when BOTH day blocks produced
    # only the "(no relevant events)" sub-line. Avoids a 5-line block
    # of dead scaffolding for a quiet day.
    if all(
        ln.strip().startswith("(no relevant events)")
        or ln.strip() in {"Today ({})".format(today.date if today else ""),
                          "Tomorrow ({})".format(
                              tomorrow.date if tomorrow else "",
                          ),
                          ""}
        for ln in parts
    ):
        return "No relevant calendar events today or tomorrow."

    return "\n".join(parts)
