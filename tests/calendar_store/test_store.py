"""Tests for CalendarStore + its summary formatter.

The calendar is context-injected — every tick, the decision prompt
includes a short "calendar context" block generated from this store.
Store schema is CALENDAR#{date} with a JSON payload containing both
economic events (macro, global) and earnings events (per-symbol).

Tests cover: write + read round-trip, missing-day returns empty,
summary filters to the strategy's symbols, summary covers today +
tomorrow (lookahead) but not arbitrary days.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.calendar_store.store import (
    CalendarDay,
    CalendarStore,
    EarningsEvent,
    EconomicEvent,
    summarize_for_symbols,
)


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── put / get round-trip ────────────────────────────────────────────


def test_put_and_get_round_trip() -> None:
    with mock_aws():
        store = CalendarStore(_table())
        day = CalendarDay(
            date="2026-04-27",
            economic=[
                EconomicEvent(
                    time_utc="14:00",
                    title="FOMC Rate Decision",
                    importance="high",
                ),
            ],
            earnings={
                "AAPL": [
                    EarningsEvent(
                        time_of_day="after_market",
                        eps_estimate="1.50",
                    ),
                ],
            },
        )
        store.put_day(day)

        loaded = store.get_day("2026-04-27")
        assert loaded is not None
        assert loaded.date == "2026-04-27"
        assert len(loaded.economic) == 1
        assert loaded.economic[0].title == "FOMC Rate Decision"
        assert "AAPL" in loaded.earnings
        assert loaded.earnings["AAPL"][0].eps_estimate == "1.50"


def test_get_day_returns_none_when_missing() -> None:
    """A day without a CALENDAR# row means the fetcher hasn't run for
    it yet. Returning None (not an empty CalendarDay) lets the caller
    decide: surface 'calendar unavailable' in context, or silently
    skip the section."""

    with mock_aws():
        store = CalendarStore(_table())
        assert store.get_day("2026-04-27") is None


def test_put_overwrites_existing() -> None:
    """Scheduled fetcher re-runs on the same day should update, not
    append. Put is write-whole-row semantics."""

    with mock_aws():
        store = CalendarStore(_table())
        first = CalendarDay(
            date="2026-04-27",
            economic=[EconomicEvent(time_utc="14:00", title="FOMC")],
            earnings={},
        )
        store.put_day(first)

        second = CalendarDay(
            date="2026-04-27",
            economic=[
                EconomicEvent(time_utc="14:00", title="FOMC (updated)"),
                EconomicEvent(time_utc="08:30", title="Initial Claims"),
            ],
            earnings={},
        )
        store.put_day(second)

        loaded = store.get_day("2026-04-27")
        assert loaded is not None
        assert len(loaded.economic) == 2


# ── summarize_for_symbols ───────────────────────────────────────────


def _sample_day(date: str) -> CalendarDay:
    return CalendarDay(
        date=date,
        economic=[
            EconomicEvent(time_utc="08:30", title="CPI", importance="high"),
            EconomicEvent(
                time_utc="14:00", title="Fed Chair Speech", importance="medium",
            ),
        ],
        earnings={
            "AAPL": [EarningsEvent(time_of_day="after_market", eps_estimate="1.50")],
            "MSFT": [EarningsEvent(time_of_day="after_market", eps_estimate="2.90")],
            "NVDA": [EarningsEvent(time_of_day="before_market", eps_estimate="0.65")],
        },
    )


def test_summary_includes_macro_events_always() -> None:
    """Macro events affect every strategy regardless of symbol — they
    must appear in every strategy's summary."""

    day = _sample_day("2026-04-27")
    summary = summarize_for_symbols(
        symbols={"AAPL"},
        today=day,
        tomorrow=None,
    )
    assert "CPI" in summary
    assert "Fed Chair Speech" in summary


def test_summary_filters_earnings_to_subscribed_symbols() -> None:
    """Earnings for symbols the strategy doesn't watch must NOT appear —
    keeps the token cost bounded and avoids red herrings for the LLM."""

    day = _sample_day("2026-04-27")
    summary = summarize_for_symbols(
        symbols={"AAPL"},
        today=day,
        tomorrow=None,
    )
    assert "AAPL" in summary
    assert "MSFT" not in summary
    assert "NVDA" not in summary


def test_summary_includes_today_and_tomorrow() -> None:
    """Strategies reacting to events want to know 'there's earnings
    in 4 hours' AND 'there's FOMC tomorrow' — one-day lookahead is
    the minimum useful window."""

    today = CalendarDay(
        date="2026-04-27",
        economic=[EconomicEvent(time_utc="14:00", title="CPI")],
        earnings={},
    )
    tomorrow = CalendarDay(
        date="2026-04-28",
        economic=[],
        earnings={
            "AAPL": [
                EarningsEvent(
                    time_of_day="before_market", eps_estimate="1.55",
                ),
            ],
        },
    )
    summary = summarize_for_symbols(
        symbols={"AAPL"}, today=today, tomorrow=tomorrow,
    )
    assert "Today" in summary
    assert "Tomorrow" in summary
    assert "CPI" in summary
    assert "AAPL" in summary


def test_summary_for_empty_day_is_concise() -> None:
    """No events today + no events tomorrow — the summary should be
    a short 'no relevant events' line, not an elaborate scaffolding
    block eating tokens."""

    empty = CalendarDay(date="2026-04-27", economic=[], earnings={})
    summary = summarize_for_symbols(
        symbols={"AAPL"}, today=empty, tomorrow=None,
    )
    # Concise — no more than a few lines.
    assert len(summary.splitlines()) <= 5
    assert "no relevant" in summary.lower() or "no events" in summary.lower()


def test_summary_when_today_is_none_means_data_unavailable() -> None:
    """If the fetcher hasn't populated today's row yet, surface that
    explicitly rather than silently pretending 'no events'. The LLM
    reasons about 'unknown' differently from 'known-empty'."""

    summary = summarize_for_symbols(
        symbols={"AAPL"}, today=None, tomorrow=None,
    )
    assert "unavailable" in summary.lower()


def test_summary_has_no_events_when_symbols_empty() -> None:
    """Strategy with dynamic symbol selection (empty list) doesn't get
    earnings — we don't know which tickers to filter for. Macro
    events still show up."""

    day = _sample_day("2026-04-27")
    summary = summarize_for_symbols(
        symbols=set(), today=day, tomorrow=None,
    )
    # Macro still present.
    assert "CPI" in summary
    # No earnings rows.
    assert "AAPL" not in summary
    assert "MSFT" not in summary
