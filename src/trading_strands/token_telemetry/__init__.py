"""Token usage telemetry — the variable-cost-per-decision metric.

LLM tokens are the biggest variable cost in the system. Every Bedrock call
emits a tuple (input_tokens, output_tokens, cost_estimate) that we record
to DynamoDB for the dashboard's cost widgets.

Schema:
    TOKEN#{org_id}#{date}              — daily org aggregate (append-safe via
                                          UpdateItem ADD). Kept indefinitely
                                          for historical billing.
    TOKEN#{org_id}#{agent_id}#{date}   — daily per-agent aggregate. Same shape.
    TOKENEVENT#{org_id}#{ts}-{rand}    — raw per-call event, 24h TTL for
                                          short-term debugging + fine-grained
                                          attribution.

The daily aggregates use DDB atomic ADD operations so concurrent writers
(multiple tick-loop recordings within the same day) converge correctly
without read-then-write races.

Cost estimation is OUR estimate based on published Bedrock per-token
pricing. Not AWS's authoritative bill. Reconciliation against the AWS
bill is a monthly operational task — discrepancies at pre-alpha scale
are expected and not blocking.
"""

from trading_strands.token_telemetry.store import (
    MODEL_PRICING,
    TokenUsage,
    TokenUsageStore,
)

__all__ = ["MODEL_PRICING", "TokenUsage", "TokenUsageStore"]
