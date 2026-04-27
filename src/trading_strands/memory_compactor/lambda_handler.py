"""AWS Lambda entry point for the end-of-day Memory Compactor.

EventBridge invokes this at ~21:30 ET on trading days, one invocation
per active Strategy Agent. The invoker passes
(org_id, agent_type, agent_id, date) in the event; agent_type defaults
to 'strategy' for v0 since that's the only agent with per-day memory
files worth compacting.

Event shape:
    {
      "org_id":     "...",
      "agent_type": "strategy" | "risk" | "compliance" | "auditor",
      "agent_id":   "...",
      "date":       "YYYY-MM-DD"         # optional; defaults to yesterday UTC
    }

Env vars expected (configured by CDK):
    DYNAMODB_TABLE             — unused for the compactor, kept for telemetry parity
    AGENT_MEMORY_BUCKET        — S3 bucket holding per-agent memory
    COMPACTOR_MODEL_ID         — Bedrock model id for the compaction call

Keeping the Lambda thin: load the memory-store adapter, build a
Strands-backed invoker, call run_compact_day, return a JSON summary.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from trading_strands.agent_memory.store import AgentMemoryStore
from trading_strands.memory_compactor.runner import run_compact_day

logger = structlog.get_logger()


def _yesterday_utc() -> str:
    import time as _time
    lt = _time.gmtime(_time.time() - 86400)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def _strands_invoker(model_id: str) -> Any:
    """Build the (system, user) -> (text, tokens_in, tokens_out) adapter
    backed by a Strands Agent. Same shape as self_critique's invoker so
    the runner-layer contract is uniform across review agents."""

    import anyio
    from strands import Agent

    def _invoke(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        agent = Agent(model=model_id, system_prompt=system_prompt)
        result = anyio.run(agent.invoke_async, user_prompt)
        text = str(result)
        usage = getattr(
            getattr(result, "metrics", None), "accumulated_usage", None,
        )
        if isinstance(usage, dict):
            return (
                text,
                int(usage.get("inputTokens", 0)),
                int(usage.get("outputTokens", 0)),
            )
        tokens_in = int(getattr(usage, "inputTokens", 0)) if usage else 0
        tokens_out = int(getattr(usage, "outputTokens", 0)) if usage else 0
        return text, tokens_in, tokens_out

    return _invoke


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry. Returns a JSON-serializable summary dict.

    The Lambda does NOT raise on a skipped-run (empty raw) — an empty
    raw file is a valid state (bot didn't decide anything that day,
    or deployed mid-day). Skipped runs are recorded in the response
    so the invoker can decide whether to alarm.
    """

    org_id = str(event["org_id"])
    agent_type = str(event.get("agent_type", "strategy"))
    agent_id = str(event["agent_id"])
    date = str(event.get("date") or _yesterday_utc())

    bucket = os.environ["AGENT_MEMORY_BUCKET"]
    model_id = os.environ.get(
        "COMPACTOR_MODEL_ID",
        "us.anthropic.claude-sonnet-4-6",
    )

    import boto3

    s3 = boto3.client("s3")
    memory = AgentMemoryStore(
        s3_client=s3,
        bucket=bucket,
        org_id=org_id,
        agent_type=agent_type,
        agent_id=agent_id,
    )

    invoker = _strands_invoker(model_id)

    report = run_compact_day(
        memory_store=memory,
        date=date,
        org_id=org_id,
        agent_type=agent_type,
        agent_id=agent_id,
        llm_invoker=invoker,
    )

    return {
        "org_id": report.org_id,
        "agent_type": report.agent_type,
        "agent_id": report.agent_id,
        "date": report.date,
        "ran": report.ran,
        "skipped_reason": report.skipped_reason,
        "raw_bytes": report.raw_bytes,
        "compressed_bytes": report.compressed_bytes,
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        "errors": report.errors,
    }
