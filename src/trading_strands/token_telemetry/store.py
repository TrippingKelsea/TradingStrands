"""DynamoDB-backed token usage store."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, NamedTuple

# Published Bedrock pricing per 1K tokens. Update when pricing changes or
# when we add new models. Our 'cost_usd_est' is input + output computed
# against these rates — an estimate until reconciled against the AWS bill.
#
# Source: AWS Bedrock pricing page (us-west-2). Keep the dict flat and
# explicit so a grep for a model ID shows exactly one line.
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # model_id: (input_per_1k_usd, output_per_1k_usd)
    "claude-opus-4-7":           (Decimal("0.015"),  Decimal("0.075")),
    "claude-opus-4-6":           (Decimal("0.015"),  Decimal("0.075")),
    "claude-sonnet-4-6":         (Decimal("0.003"),  Decimal("0.015")),
    "claude-haiku-4-5":          (Decimal("0.0008"), Decimal("0.004")),
    # Fallback: if we don't recognize the model, both rates are 0. This is
    # deliberate — we'd rather surface "unknown cost" than invent a price.
}


def _price_for(model_id: str) -> tuple[Decimal, Decimal]:
    """Return (input, output) per-1k-token price in USD for a model, or
    (0, 0) if we don't have pricing for it. Caller should surface that
    as 'unknown' in the UI rather than silently treating as free."""

    # Loose match: some callers pass the fully-qualified Bedrock model
    # identifier (e.g., 'us.anthropic.claude-sonnet-4-6'). Find the best
    # suffix match in our table.
    for known, pricing in MODEL_PRICING.items():
        if known in model_id:
            return pricing
    return Decimal("0"), Decimal("0")


class TokenUsage(NamedTuple):
    org_id: str
    agent_id: str
    agent_type: str  # 'strategy', 'compiler', etc.
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd_est: Decimal
    timestamp: int


def compute_cost(
    model: str, input_tokens: int, output_tokens: int,
) -> Decimal:
    """Compute estimated USD cost for a single invocation."""

    in_rate, out_rate = _price_for(model)
    return (
        Decimal(input_tokens) / Decimal(1000) * in_rate
        + Decimal(output_tokens) / Decimal(1000) * out_rate
    )


def _date_key(ts: int) -> str:
    lt = time.gmtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


class TokenUsageStore:
    """Records a TokenUsage event to three DDB items:
      - TOKEN#{org_id}#{date}: daily org aggregate (ADD)
      - TOKEN#{org_id}#{agent_id}#{date}: daily per-agent aggregate (ADD)
      - TOKENEVENT#{org_id}#{ts}-{rand}: raw event, 24h TTL

    Atomic ADD lets concurrent writers converge — no read-modify-write
    race. An agent burning tokens in parallel with another just accumulates
    into the same daily bucket.
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    def record(self, usage: TokenUsage) -> None:
        date = _date_key(usage.timestamp)

        # Org daily aggregate
        self._table.update_item(
            Key={"pk": f"TOKEN#{usage.org_id}#{date}"},
            UpdateExpression=(
                "ADD input_tokens :i, output_tokens :o, "
                "cost_usd_est :c, invocations :one "
                "SET org_id = if_not_exists(org_id, :org), "
                "#d = if_not_exists(#d, :date)"
            ),
            ExpressionAttributeNames={"#d": "date"},
            ExpressionAttributeValues={
                ":i": usage.input_tokens,
                ":o": usage.output_tokens,
                ":c": usage.cost_usd_est,
                ":one": 1,
                ":org": usage.org_id,
                ":date": date,
            },
        )

        # Per-agent daily aggregate
        self._table.update_item(
            Key={"pk": f"TOKEN#{usage.org_id}#{usage.agent_id}#{date}"},
            UpdateExpression=(
                "ADD input_tokens :i, output_tokens :o, "
                "cost_usd_est :c, invocations :one "
                "SET org_id = if_not_exists(org_id, :org), "
                "agent_id = if_not_exists(agent_id, :aid), "
                "agent_type = if_not_exists(agent_type, :atype), "
                "#d = if_not_exists(#d, :date)"
            ),
            ExpressionAttributeNames={"#d": "date"},
            ExpressionAttributeValues={
                ":i": usage.input_tokens,
                ":o": usage.output_tokens,
                ":c": usage.cost_usd_est,
                ":one": 1,
                ":org": usage.org_id,
                ":aid": usage.agent_id,
                ":atype": usage.agent_type,
                ":date": date,
            },
        )

        # Raw event, 24h TTL
        event_id = f"{usage.timestamp}-{uuid.uuid4().hex[:6]}"
        self._table.put_item(Item={
            "pk": f"TOKENEVENT#{usage.org_id}#{event_id}",
            "org_id": usage.org_id,
            "agent_id": usage.agent_id,
            "agent_type": usage.agent_type,
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd_est": usage.cost_usd_est,
            "timestamp": usage.timestamp,
            "ttl": usage.timestamp + 86400,
        })

    def daily_org_total(
        self, org_id: str, date: str,
    ) -> dict[str, Any]:
        """Return today's org-wide tokens + cost summary."""

        resp = self._table.get_item(Key={"pk": f"TOKEN#{org_id}#{date}"})
        return dict(resp.get("Item", {}))

    def daily_agent_total(
        self, org_id: str, agent_id: str, date: str,
    ) -> dict[str, Any]:
        resp = self._table.get_item(
            Key={"pk": f"TOKEN#{org_id}#{agent_id}#{date}"},
        )
        return dict(resp.get("Item", {}))
