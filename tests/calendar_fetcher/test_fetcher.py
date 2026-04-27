"""Tests for the calendar fetcher Lambda's pure-function core.

The fetcher calls Finnhub for earnings + economic events, assembles
a CalendarDay, writes it to CalendarStore. HTTP is abstracted behind
a small client so tests inject a fake without mocking stdlib.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.calendar_fetcher.fetcher import (
    FinnhubClient,
    build_calendar_day,
    fetch_and_store_day,
)
from trading_strands.calendar_store.store import CalendarStore


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── build_calendar_day ─────────────────────────────────────────────


def test_build_day_from_finnhub_response_shapes() -> None:
    """Given raw Finnhub-shaped payloads, produce a CalendarDay that
    round-trips through the store. Uses documented Finnhub field names."""

    earnings_payload = {
        "earningsCalendar": [
            {
                "date": "2026-04-27", "symbol": "AAPL",
                "hour": "amc", "epsEstimate": 1.50,
                "revenueEstimate": 96000000000,
            },
            {
                "date": "2026-04-27", "symbol": "MSFT",
                "hour": "amc", "epsEstimate": 2.90,
            },
            # Different date — must be filtered out.
            {
                "date": "2026-04-28", "symbol": "NVDA",
                "hour": "bmo", "epsEstimate": 0.65,
            },
        ],
    }
    economic_payload = {
        "economicCalendar": [
            {
                "time": "2026-04-27 14:00:00",
                "event": "FOMC Rate Decision",
                "impact": "high", "country": "US",
            },
            {
                "time": "2026-04-27 12:30:00",
                "event": "Initial Jobless Claims",
                "impact": "medium", "country": "US",
            },
            # Different date — filtered out.
            {
                "time": "2026-04-28 14:00:00",
                "event": "GDP", "impact": "high", "country": "US",
            },
        ],
    }

    day = build_calendar_day(
        date="2026-04-27",
        earnings_payload=earnings_payload,
        economic_payload=economic_payload,
    )

    assert day.date == "2026-04-27"
    # Earnings filtered to the date, keyed by symbol.
    assert set(day.earnings.keys()) == {"AAPL", "MSFT"}
    assert day.earnings["AAPL"][0].time_of_day == "after_market"
    # Finnhub returns numeric JSON; we coerce to str, but value matters
    # more than exact formatting ("1.5" vs "1.50" from float repr).
    assert day.earnings["AAPL"][0].eps_estimate is not None
    assert float(day.earnings["AAPL"][0].eps_estimate) == 1.50
    # Economic filtered to the date.
    assert len(day.economic) == 2
    titles = {e.title for e in day.economic}
    assert titles == {"FOMC Rate Decision", "Initial Jobless Claims"}
    assert any(e.importance == "high" for e in day.economic)


def test_build_day_translates_bmo_amc_hour_codes() -> None:
    """Finnhub uses 'bmo'/'amc' for before/after market; our store
    normalizes to 'before_market'/'after_market' (§3.1)."""

    payload = {"earningsCalendar": [
        {"date": "2026-04-27", "symbol": "AAPL", "hour": "bmo"},
        {"date": "2026-04-27", "symbol": "MSFT", "hour": "amc"},
        {"date": "2026-04-27", "symbol": "NVDA", "hour": "dmh"},  # unknown
    ]}
    day = build_calendar_day(
        date="2026-04-27",
        earnings_payload=payload,
        economic_payload={"economicCalendar": []},
    )
    assert day.earnings["AAPL"][0].time_of_day == "before_market"
    assert day.earnings["MSFT"][0].time_of_day == "after_market"
    # Unknown code kept as-is — no silent data loss.
    assert day.earnings["NVDA"][0].time_of_day == "dmh"


def test_build_day_handles_empty_payloads() -> None:
    day = build_calendar_day(
        date="2026-04-27",
        earnings_payload={},
        economic_payload={},
    )
    assert day.date == "2026-04-27"
    assert day.economic == []
    assert day.earnings == {}


def test_build_day_tolerates_missing_estimates() -> None:
    """Partial data is still useful — a strategy wants to know AAPL
    reports today even without a published EPS estimate."""

    payload = {"earningsCalendar": [
        {"date": "2026-04-27", "symbol": "AAPL", "hour": "amc"},
    ]}
    day = build_calendar_day(
        date="2026-04-27",
        earnings_payload=payload,
        economic_payload={"economicCalendar": []},
    )
    assert day.earnings["AAPL"][0].eps_estimate is None


# ── fetch_and_store_day ────────────────────────────────────────────


class _StubClient:
    """In-process FinnhubClient replacement. Records calls, returns
    canned responses."""

    def __init__(
        self,
        earnings: dict[str, Any] | None = None,
        economic: dict[str, Any] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self._earnings = earnings or {"earningsCalendar": []}
        self._economic = economic or {"economicCalendar": []}
        self._raise_on = raise_on
        self.calls: list[tuple[str, str, str]] = []  # (endpoint, from, to)

    def earnings_calendar(
        self, date_from: str, date_to: str,
    ) -> dict[str, Any]:
        self.calls.append(("earnings", date_from, date_to))
        if self._raise_on == "earnings":
            raise RuntimeError("finnhub down")
        return self._earnings

    def economic_calendar(
        self, date_from: str, date_to: str,
    ) -> dict[str, Any]:
        self.calls.append(("economic", date_from, date_to))
        if self._raise_on == "economic":
            raise RuntimeError("finnhub down")
        return self._economic


def test_fetch_and_store_end_to_end() -> None:
    with mock_aws():
        cal_store = CalendarStore(_table())
        client = _StubClient(
            earnings={"earningsCalendar": [
                {"date": "2026-04-27", "symbol": "AAPL", "hour": "amc"},
            ]},
            economic={"economicCalendar": [
                {
                    "time": "2026-04-27 14:00:00",
                    "event": "CPI", "impact": "high",
                },
            ]},
        )

        fetch_and_store_day(
            date="2026-04-27",
            client=client,
            calendar_store=cal_store,
        )

        loaded = cal_store.get_day("2026-04-27")
        assert loaded is not None
        assert "AAPL" in loaded.earnings
        assert len(loaded.economic) == 1


def test_fetch_and_store_raises_when_both_endpoints_fail() -> None:
    """One endpoint failing is recoverable (we write partial data).
    Both failing means we have no data to store — raise so the
    scheduled invocation logs an error EventBridge can alert on."""

    import pytest

    with mock_aws():
        cal_store = CalendarStore(_table())

        class _AllFail:
            def earnings_calendar(self, *a: Any) -> dict[str, Any]:
                raise RuntimeError("earnings down")

            def economic_calendar(self, *a: Any) -> dict[str, Any]:
                raise RuntimeError("economic down")

        with pytest.raises(RuntimeError):
            fetch_and_store_day(
                date="2026-04-27",
                client=_AllFail(),
                calendar_store=cal_store,
            )


def test_fetch_and_store_proceeds_with_partial_data() -> None:
    """One endpoint up, one down — write what we got. Better partial
    calendar than no calendar."""

    with mock_aws():
        cal_store = CalendarStore(_table())
        client = _StubClient(
            earnings={"earningsCalendar": [
                {"date": "2026-04-27", "symbol": "AAPL", "hour": "amc"},
            ]},
            raise_on="economic",
        )
        fetch_and_store_day(
            date="2026-04-27",
            client=client,
            calendar_store=cal_store,
        )
        loaded = cal_store.get_day("2026-04-27")
        assert loaded is not None
        # Earnings landed.
        assert "AAPL" in loaded.earnings
        # Economic failed → empty list (not None).
        assert loaded.economic == []


# ── FinnhubClient signature shape (doesn't do network) ─────────────


def test_finnhub_client_constructs_with_api_key() -> None:
    """Sanity check on the class shape. Actual network calls are
    tested at the handler level against a live stub."""

    client = FinnhubClient(api_key="test-key")
    assert client.api_key == "test-key"
