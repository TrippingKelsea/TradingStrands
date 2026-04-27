"""Memory compactor runner.

Pure-function core. The runner:
    1. Reads the raw daily markdown for (org_id, agent_type, agent_id, date).
    2. Skips when the raw file is empty (no memory to compact).
    3. Invokes an LLM via the passed-in invoker shape used by every
       other review agent:  (system, user) -> (text, tokens_in, tokens_out).
    4. Writes the compressed sibling.
    5. Never touches the raw file.

The LLM invoker is injected so tests exercise the pipeline without
Bedrock. In production the Lambda handler wires Strands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


COMPACTOR_SYSTEM_PROMPT = """\
You are a memory compactor for an automated trading agent.

You will receive the raw markdown memory for one trading day. Produce
a compressed version that preserves the DECISION TRACE while dropping
fine-grained tick-level noise. The audience is:
  - the agent itself, tomorrow (for next-day context);
  - the weekend Self-Critique Agent (reasoning over a week's memory);
  - the chat feature (answering "what did we do yesterday?").

Preserve, with their timestamps + data pointers:
  - Every BUY / SELL / CLOSE action, the rationale, the outcome.
  - HOLD and NOOP decisions where the rationale shifted vs prior days.
  - Ledger-moving events (fills, fee lines, halts).
  - Any confabulation flags (claims without MARKETDATA#/LEDGER#/
    DECISION# pointers — keep them so critique can see them).

Drop:
  - Repeated "no signal, noop" lines from adjacent ticks.
  - Verbose market-data snapshots that duplicate the MARKETDATA# pointer.
  - Model reasoning that does not affect the decision.

CRITICAL:
  - Preserve all data pointers verbatim. Do not invent pointers.
  - Do not drop decisions. Summarizing ≠ deleting history.
  - If a section is already brief, re-emit it as-is rather than
    pretending to summarize.
  - Output markdown. Keep the day's header. Maintain chronological order.
"""


@dataclass
class CompactReport:
    """Output of one compaction invocation."""

    org_id: str
    agent_type: str
    agent_id: str
    date: str  # YYYY-MM-DD
    tokens_in: int = 0
    tokens_out: int = 0
    raw_bytes: int = 0
    compressed_bytes: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        """True iff the compactor actually invoked the LLM + wrote a file.

        Skipped (empty raw, error) runs return False — caller can use
        this to decide whether to emit a metric or alarm on repeated
        skips.
        """

        return self.skipped_reason == "" and not self.errors


def run_compact_day(
    *,
    memory_store: Any,
    date: str,
    org_id: str,
    agent_type: str,
    agent_id: str,
    llm_invoker: Any,
) -> CompactReport:
    """Compact one day's raw memory into a compressed sibling.

    Idempotent: re-running overwrites the compressed file with a fresh
    summary of the same raw source. The raw file is never touched.
    """

    report = CompactReport(
        org_id=org_id, agent_type=agent_type, agent_id=agent_id, date=date,
    )

    try:
        raw = memory_store.read_day(date)
    except Exception as exc:
        report.errors.append(f"memory.read_day: {exc}")
        return report

    report.raw_bytes = len(raw.encode("utf-8"))
    if not raw.strip():
        report.skipped_reason = "empty_raw"
        logger.info(
            "compactor.skipped org_id=%s agent_id=%s date=%s reason=empty_raw",
            org_id, agent_id, date,
        )
        return report

    user_prompt = (
        f"# Raw memory for {date} (org {org_id}, agent {agent_id})\n\n"
        f"{raw}"
    )

    try:
        text, tokens_in, tokens_out = llm_invoker(
            COMPACTOR_SYSTEM_PROMPT, user_prompt,
        )
    except Exception as exc:
        report.errors.append(f"llm_invoker: {exc}")
        return report

    report.tokens_in = tokens_in
    report.tokens_out = tokens_out
    compressed = text.strip() + "\n"
    report.compressed_bytes = len(compressed.encode("utf-8"))

    try:
        memory_store.write_compressed(date, compressed)
    except Exception as exc:
        report.errors.append(f"memory.write_compressed: {exc}")
        return report

    logger.info(
        "compactor.complete org_id=%s agent_id=%s date=%s tokens_in=%d tokens_out=%d",
        org_id, agent_id, date, tokens_in, tokens_out,
    )
    return report
