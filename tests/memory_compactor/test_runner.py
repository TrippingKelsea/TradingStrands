"""Tests for the memory compactor runner.

Uses an in-memory stub store so the whole pipeline exercises without
S3. The LLM is a canned-response callable. These tests verify the
boundary invariants from docs/SPEC/agent_memory.md:
  - Raw file is never modified.
  - Compressed file is written idempotently.
  - Empty-raw is skipped, not a failure.
  - LLM errors produce a report with errors, no compressed write.
"""

from __future__ import annotations

from trading_strands.memory_compactor.runner import (
    COMPACTOR_SYSTEM_PROMPT,
    run_compact_day,
)


class FakeMemoryStore:
    """Matches the two methods the compactor uses on AgentMemoryStore:
    read_day (raw) + write_compressed. Other methods are absent on
    purpose — the compactor must not touch them."""

    def __init__(self) -> None:
        self.raw: dict[str, str] = {}
        self.compressed: dict[str, str] = {}

    def read_day(self, date: str) -> str:
        return self.raw.get(date, "")

    def write_compressed(self, date: str, content: str) -> None:
        self.compressed[date] = content


def _canned_invoker(
    response: str = "# Compressed\n- summary line\n",
    tokens_in: int = 100,
    tokens_out: int = 40,
):
    def _invoke(system_prompt: str, user_prompt: str):
        # Verify system prompt is being passed through so assertions
        # about its content aren't trivially defeated.
        assert "trading agent" in system_prompt
        return response, tokens_in, tokens_out
    return _invoke


def test_full_run_writes_compressed_leaves_raw_untouched() -> None:
    store = FakeMemoryStore()
    store.raw["2026-04-26"] = "- 09:30 BUY SPY x10 ..."

    report = run_compact_day(
        memory_store=store,
        date="2026-04-26",
        org_id="org-1", agent_type="strategy", agent_id="s-1",
        llm_invoker=_canned_invoker(),
    )
    assert report.ran is True
    assert store.raw["2026-04-26"] == "- 09:30 BUY SPY x10 ..."
    assert "summary line" in store.compressed["2026-04-26"]


def test_empty_raw_is_skipped_not_failed() -> None:
    """No memory that day → skip. Not an error — a bot that didn't
    decide anything is a valid state (deployed mid-day, off-hours).
    Surfacing it as an error would clutter CloudWatch with false
    positives."""

    store = FakeMemoryStore()
    report = run_compact_day(
        memory_store=store,
        date="2026-04-26",
        org_id="org-1", agent_type="strategy", agent_id="s-1",
        llm_invoker=_canned_invoker(),
    )
    assert report.ran is False
    assert report.skipped_reason == "empty_raw"
    assert "2026-04-26" not in store.compressed
    assert not report.errors


def test_llm_error_produces_error_report_no_compressed_write() -> None:
    store = FakeMemoryStore()
    store.raw["2026-04-26"] = "- raw content"

    def _bad_invoker(_s, _u):
        raise RuntimeError("bedrock bad day")

    report = run_compact_day(
        memory_store=store,
        date="2026-04-26",
        org_id="org-1", agent_type="strategy", agent_id="s-1",
        llm_invoker=_bad_invoker,
    )
    assert report.ran is False
    assert report.errors
    assert "bedrock bad day" in report.errors[0]
    assert "2026-04-26" not in store.compressed


def test_idempotent_re_run_overwrites_compressed() -> None:
    """Running twice is safe: the second run overwrites the compressed
    file with a fresh summary of the same raw source."""

    store = FakeMemoryStore()
    store.raw["2026-04-26"] = "- raw"

    run_compact_day(
        memory_store=store, date="2026-04-26",
        org_id="o", agent_type="strategy", agent_id="s",
        llm_invoker=_canned_invoker(response="v1"),
    )
    assert store.compressed["2026-04-26"].strip() == "v1"

    run_compact_day(
        memory_store=store, date="2026-04-26",
        org_id="o", agent_type="strategy", agent_id="s",
        llm_invoker=_canned_invoker(response="v2"),
    )
    assert store.compressed["2026-04-26"].strip() == "v2"


def test_token_counts_recorded() -> None:
    store = FakeMemoryStore()
    store.raw["2026-04-26"] = "- raw"

    report = run_compact_day(
        memory_store=store, date="2026-04-26",
        org_id="o", agent_type="strategy", agent_id="s",
        llm_invoker=_canned_invoker(tokens_in=1200, tokens_out=300),
    )
    assert report.tokens_in == 1200
    assert report.tokens_out == 300


def test_system_prompt_preserves_pointers_and_decisions() -> None:
    """Invariants the compactor prompt must enforce so compressed
    memory stays audit-useful:
      - keep all data pointers verbatim
      - don't drop decisions
      - don't invent anything"""

    assert "pointers verbatim" in COMPACTOR_SYSTEM_PROMPT
    assert "Do not drop decisions" in COMPACTOR_SYSTEM_PROMPT
    assert "Do not invent" in COMPACTOR_SYSTEM_PROMPT


def test_read_day_failure_surfaces_as_error() -> None:
    class Broken:
        def read_day(self, _: str) -> str:
            raise RuntimeError("s3 nope")

        def write_compressed(self, *args, **kwargs) -> None:
            raise AssertionError("should not be called")

    report = run_compact_day(
        memory_store=Broken(), date="2026-04-26",
        org_id="o", agent_type="strategy", agent_id="s",
        llm_invoker=_canned_invoker(),
    )
    assert report.ran is False
    assert report.errors
    assert "s3 nope" in report.errors[0]
