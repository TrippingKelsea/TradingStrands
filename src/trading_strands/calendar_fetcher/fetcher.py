"""Calendar fetcher — scheduled Lambda.

Calls Finnhub's earnings + economic endpoints for the target date
(defaults to today), translates the responses into our CalendarDay
shape, writes to CalendarStore.

Resilience posture:
  - Partial data beats no data: if one endpoint fails and the other
    succeeds, write the successful half. CalendarDay with an empty
    economic list is legitimate.
  - Both endpoints failing is unrecoverable for this cycle — raise
    so the scheduled invocation logs an error EventBridge can alert
    on later.

HTTP is abstracted behind FinnhubClient so unit tests inject a stub
without needing live network access.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

import structlog

from trading_strands.calendar_store.store import (
    CalendarDay,
    CalendarStore,
    EarningsEvent,
    EconomicEvent,
)

logger = structlog.get_logger()

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Finnhub uses compact hour codes; our store uses full strings.
_HOUR_CODE_TO_LABEL = {
    "bmo": "before_market",
    "amc": "after_market",
    # Anything else we keep verbatim so the operator can see it.
}

# Finnhub event "impact" field → our "importance". Conservative
# mapping; unknown values pass through.
_IMPACT_TO_IMPORTANCE = {
    "high": "high", "medium": "medium", "low": "low",
    "3": "high", "2": "medium", "1": "low",
}


class FinnhubClient:
    """Minimal HTTP client for Finnhub's two calendar endpoints.

    urllib.request instead of requests/httpx — one GET per endpoint,
    no auth flows beyond a query-string API key. Stdlib is enough
    and keeps the Lambda cold-start lean.
    """

    def __init__(self, api_key: str, base_url: str = FINNHUB_BASE) -> None:
        self.api_key = api_key
        self._base = base_url

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        # URL is constructed from a fixed base + finnhub-specific
        # query string; scheme is https, not attacker-controlled.
        # Silencing S310 on both the Request + urlopen calls —
        # fixed scheme means the file:/ custom-scheme warning
        # doesn't apply here.
        params = {**params, "token": self.api_key}
        url = f"{self._base}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(  # noqa: S310
            url, headers={"User-Agent": "TradingStrands calendar-fetcher"},
        )
        # Short timeout — the scheduler retries per day, and a hung
        # Lambda burns $ without landing data.
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = resp.read()
        parsed: dict[str, Any] = json.loads(body.decode("utf-8"))
        return parsed

    def earnings_calendar(
        self, date_from: str, date_to: str,
    ) -> dict[str, Any]:
        return self._get(
            "calendar/earnings",
            {"from": date_from, "to": date_to},
        )

    def economic_calendar(
        self, date_from: str, date_to: str,
    ) -> dict[str, Any]:
        return self._get(
            "calendar/economic",
            {"from": date_from, "to": date_to},
        )


def _translate_hour_code(code: str | None) -> str:
    if not code:
        return "unknown"
    return _HOUR_CODE_TO_LABEL.get(code, code)


def _translate_impact(impact: Any) -> str:
    if impact is None:
        return "medium"
    return _IMPACT_TO_IMPORTANCE.get(str(impact).lower(), "medium")


def _translate_earnings(
    items: list[dict[str, Any]], date: str,
) -> dict[str, list[EarningsEvent]]:
    out: dict[str, list[EarningsEvent]] = {}
    for item in items:
        if str(item.get("date")) != date:
            continue
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        ev = EarningsEvent(
            time_of_day=_translate_hour_code(item.get("hour")),
            eps_estimate=(
                str(item["epsEstimate"])
                if item.get("epsEstimate") is not None else None
            ),
            revenue_estimate=(
                str(item["revenueEstimate"])
                if item.get("revenueEstimate") is not None else None
            ),
        )
        out.setdefault(symbol, []).append(ev)
    return out


def _translate_economic(
    items: list[dict[str, Any]], date: str,
) -> list[EconomicEvent]:
    out: list[EconomicEvent] = []
    for item in items:
        raw_time = str(item.get("time", ""))
        # Finnhub format: "YYYY-MM-DD HH:MM:SS" in UTC.
        if not raw_time.startswith(date):
            continue
        # Pull HH:MM. If the string is malformed fall back to "00:00"
        # rather than drop the event — operators still want to see
        # the event existed.
        time_part = raw_time.split(" ")[1] if " " in raw_time else "00:00:00"
        hhmm = time_part[:5] if len(time_part) >= 5 else "00:00"
        out.append(EconomicEvent(
            time_utc=hhmm,
            title=str(item.get("event", "")),
            importance=_translate_impact(item.get("impact")),
            country=str(item.get("country", "US")),
        ))
    return out


def build_calendar_day(
    date: str,
    earnings_payload: dict[str, Any],
    economic_payload: dict[str, Any],
) -> CalendarDay:
    """Translate raw Finnhub responses into a CalendarDay for the
    given date. Items for other dates in the payloads are filtered
    out so a single fetch over a wider range doesn't leak across
    rows."""

    earnings_items = (
        earnings_payload.get("earningsCalendar") or []
        if isinstance(earnings_payload, dict) else []
    )
    economic_items = (
        economic_payload.get("economicCalendar") or []
        if isinstance(economic_payload, dict) else []
    )
    return CalendarDay(
        date=date,
        economic=_translate_economic(economic_items, date),
        earnings=_translate_earnings(earnings_items, date),
    )


def fetch_and_store_day(
    date: str,
    client: Any,
    calendar_store: CalendarStore,
) -> None:
    """Fetch both endpoints, build a CalendarDay, write it.

    Partial success is OK — if one endpoint fails we store what we
    got from the other. Total failure raises.
    """

    earnings_payload: dict[str, Any] = {}
    economic_payload: dict[str, Any] = {}
    earnings_err: Exception | None = None
    economic_err: Exception | None = None

    try:
        earnings_payload = client.earnings_calendar(date, date)
    except Exception as exc:
        earnings_err = exc
        logger.exception("calendar.earnings_fetch_failed", date=date)

    try:
        economic_payload = client.economic_calendar(date, date)
    except Exception as exc:
        economic_err = exc
        logger.exception("calendar.economic_fetch_failed", date=date)

    if earnings_err is not None and economic_err is not None:
        msg = (
            f"calendar fetch failed for {date}: "
            f"earnings={earnings_err} economic={economic_err}"
        )
        raise RuntimeError(msg)

    day = build_calendar_day(
        date=date,
        earnings_payload=earnings_payload,
        economic_payload=economic_payload,
    )
    calendar_store.put_day(day)
    logger.info(
        "calendar.stored",
        date=date,
        economic_count=len(day.economic),
        earnings_count=sum(len(v) for v in day.earnings.values()),
    )


def _today_utc() -> str:
    lt = time.gmtime()
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point.

    Event shape (all optional):
        { "date": "YYYY-MM-DD" }  — fetch for a specific date;
                                    defaults to today UTC
    """

    import boto3

    date = str(event.get("date") or _today_utc())
    table_name = os.environ["DYNAMODB_TABLE"]
    secret_name = os.environ["CALENDAR_SECRET_NAME"]

    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_name)
    payload = json.loads(resp.get("SecretString", "{}"))
    api_key = payload.get("FINNHUB_API_KEY", "")
    if not api_key:
        msg = (
            f"CALENDAR_SECRET_NAME={secret_name} missing FINNHUB_API_KEY"
        )
        raise RuntimeError(msg)

    ddb = boto3.resource("dynamodb")
    table = ddb.Table(table_name)
    client = FinnhubClient(api_key=api_key)
    fetch_and_store_day(
        date=date,
        client=client,
        calendar_store=CalendarStore(table),
    )
    return {"ok": True, "date": date}
