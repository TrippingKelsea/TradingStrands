"""Tests for AgentMemoryStore.

Coverage: append/read roundtrip for daily + lessons files, compressed
fallback semantics, recent-days helper, path isolation between Agents.
"""

from __future__ import annotations

from typing import Any

from trading_strands.agent_memory.store import (
    AgentMemoryStore,
    DailyFileKey,
    today_utc,
)

BUCKET = "trading-strands-agent-memory-test"


def _store(s3: Any, org: str = "org-a", agent: str = "bot-1") -> AgentMemoryStore:
    return AgentMemoryStore(
        s3_client=s3, bucket=BUCKET,
        org_id=org, agent_type="strategy", agent_id=agent,
    )


def test_daily_key_format() -> None:
    k = DailyFileKey("org-a", "strategy", "bot-1", "2026-04-26", "raw")
    assert k.s3_key == "org-a/strategy/bot-1/2026-04-26.md"
    k2 = k._replace(kind="compressed")
    assert k2.s3_key == "org-a/strategy/bot-1/2026-04-26.compressed.md"


def test_today_utc_returns_yyyy_mm_dd() -> None:
    d = today_utc()
    assert len(d) == 10
    assert d[4] == "-" and d[7] == "-"


def test_read_missing_returns_empty(s3_client: Any) -> None:
    store = _store(s3_client)
    assert store.read_day("2026-04-26") == ""
    assert store.read_lessons() == ""


def test_append_to_today_then_read(s3_client: Any) -> None:
    store = _store(s3_client)
    store.append_to_today("## Actions")
    store.append_to_today("- bought AAPL at 150")
    content = store.read_day(today_utc())
    assert "## Actions" in content
    assert "bought AAPL at 150" in content
    # Each block ends with a newline.
    assert content.count("\n") >= 2


def test_append_preserves_ordering(s3_client: Any) -> None:
    store = _store(s3_client)
    store.append_to_today("first")
    store.append_to_today("second")
    store.append_to_today("third")
    content = store.read_day(today_utc())
    first_pos = content.index("first")
    second_pos = content.index("second")
    third_pos = content.index("third")
    assert first_pos < second_pos < third_pos


def test_append_lesson_and_read(s3_client: Any) -> None:
    store = _store(s3_client)
    store.append_lesson("## 2026-04-20")
    store.append_lesson("Post-FOMC opens chop for 20 min.")
    content = store.read_lessons()
    assert "## 2026-04-20" in content
    assert "Post-FOMC" in content


def test_append_recommendation_and_read(s3_client: Any) -> None:
    """Review agents (Risk/Compliance/Auditor) write to a distinct file
    so the UI can surface recommendations separately from lessons."""

    store = _store(s3_client)
    assert store.read_recommendations() == ""
    store.append_recommendation("## 2026-04-26 — risk review")
    store.append_recommendation(
        "Concentration in NVDA exceeds 40% of equity.",
    )
    content = store.read_recommendations()
    assert "## 2026-04-26" in content
    assert "NVDA" in content
    # Lessons file is untouched — separate stream.
    assert store.read_lessons() == ""


def test_compressed_falls_back_to_raw_when_missing(s3_client: Any) -> None:
    """If the end-of-day compactor hasn't run, reading compressed
    returns the raw daily content so downstream readers aren't
    blocked on compaction."""

    store = _store(s3_client)
    store.append_to_today("- raw observation")
    date = today_utc()
    compressed = store.read_compressed(date)
    assert "raw observation" in compressed


def test_write_compressed_preferred_over_raw(s3_client: Any) -> None:
    """Once the compactor has written a compressed file, read_compressed
    returns THAT, not the raw."""

    store = _store(s3_client)
    store.append_to_today("raw line A")
    store.append_to_today("raw line B")
    store.write_compressed(today_utc(), "Summary: agent did X and Y.")

    compressed = store.read_compressed(today_utc())
    assert "Summary" in compressed
    assert "raw line A" not in compressed
    # Raw file is unaffected.
    raw = store.read_day(today_utc())
    assert "raw line A" in raw


def test_agents_isolated_by_path(s3_client: Any) -> None:
    """Two Agents in the same org + type have different prefixes and
    cannot see each other's memory."""

    a = _store(s3_client, agent="bot-1")
    b = _store(s3_client, agent="bot-2")
    a.append_to_today("private to A")
    b.append_to_today("private to B")
    assert "private to A" in a.read_day(today_utc())
    assert "private to B" not in a.read_day(today_utc())
    assert "private to A" not in b.read_day(today_utc())


def test_orgs_isolated_by_path(s3_client: Any) -> None:
    """Agent 'bot-1' exists in two different orgs — independent memories."""

    org_a = _store(s3_client, org="org-a", agent="bot-1")
    org_b = _store(s3_client, org="org-b", agent="bot-1")
    org_a.append_to_today("in org A")
    org_b.append_to_today("in org B")
    assert "in org A" in org_a.read_day(today_utc())
    assert "in org A" not in org_b.read_day(today_utc())


def test_load_recent_days_returns_newest_first(s3_client: Any) -> None:
    store = _store(s3_client)
    # Simulate some past days by writing compressed files directly.
    store.write_compressed("2026-04-24", "Wed summary")
    store.write_compressed("2026-04-25", "Thu summary")
    store.write_compressed("2026-04-26", "Fri summary")

    days = store.load_recent_days(count=3, end_date="2026-04-26")
    assert len(days) == 3
    # Newest first.
    assert days[0][0] == "2026-04-26"
    assert "Fri summary" in days[0][1]
    assert days[2][0] == "2026-04-24"
    assert "Wed summary" in days[2][1]


def test_load_recent_days_includes_missing_as_empty(s3_client: Any) -> None:
    """Gaps (days the agent didn't run) appear as empty strings in the
    result. Caller decides whether to skip — Self-Critique Agent wants
    to see gaps; chat feature may skip them."""

    store = _store(s3_client)
    store.write_compressed("2026-04-24", "present")
    # 2026-04-25 skipped
    store.write_compressed("2026-04-26", "also present")

    days = store.load_recent_days(count=3, end_date="2026-04-26")
    by_date = dict(days)
    assert by_date["2026-04-26"] == "also present"
    assert by_date["2026-04-25"] == ""
    assert by_date["2026-04-24"] == "present"
