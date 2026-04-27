"""Allowlist of LLM model IDs a strategy author can pick from.

Source of truth for "which models is TradingStrands willing to run".
Validation at strategy save time protects against silent cost /
behavioral surprises: an unknown model ID would either (a) crash at
bot-start, (b) succeed but price at $0 in cost telemetry, or (c)
behave differently than the author expected. All three are worse
than "sorry, pick from this list".

Bedrock models need a corresponding entry in
trading_strands.token_telemetry.store.MODEL_PRICING so the cost
dashboard is accurate. A test (`test_all_bedrock_models_have_pricing`)
enforces this.

OpenAI / GPT entries are declared here but not yet wired into the
StrategyBot's Strands Agent construction — that's a separate
commit (needs org-level OPENAI_API_KEY + Strands' OpenAI adapter).
Declaring them now means the UI + data model don't have to change
when the wiring lands.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnknownModelError(ValueError):
    """Raised when a model_id isn't in the allowlist."""


@dataclass(frozen=True)
class ModelChoice:
    """One model the UI offers. `id` is the value stored on the
    strategy; `label` is what the dropdown shows."""

    id: str
    label: str
    provider: str     # "bedrock" | "openai" | "other"
    description: str = ""


# Order matters for UI display — roughly default-first, then by
# provider, then by cost/capability.
_ALLOWLIST: tuple[ModelChoice, ...] = (
    ModelChoice(
        id="us.anthropic.claude-sonnet-4-6",
        label="Claude Sonnet 4.6 (default)",
        provider="bedrock",
        description="Balanced. Good cost/quality baseline for most strategies.",
    ),
    ModelChoice(
        id="us.anthropic.claude-opus-4-7",
        label="Claude Opus 4.7",
        provider="bedrock",
        description="Highest quality, 5x Sonnet cost. Consider for complex strategies.",
    ),
    ModelChoice(
        id="us.anthropic.claude-opus-4-6",
        label="Claude Opus 4.6",
        provider="bedrock",
        description="Previous-generation Opus. Useful for A/B comparison against 4.7.",
    ),
    ModelChoice(
        id="us.anthropic.claude-haiku-4-5",
        label="Claude Haiku 4.5",
        provider="bedrock",
        description="Fastest, cheapest Anthropic model on Bedrock. "
                    "Test before production — quality trade-offs can be strategy-specific.",
    ),
    ModelChoice(
        id="us.amazon.nova-pro-v1:0",
        label="Amazon Nova Pro",
        provider="bedrock",
        description="Amazon's flagship. Cheaper than Claude Sonnet; "
                    "useful for cross-family comparison.",
    ),
    # OpenAI entries: DECLARED but not yet wired into bot construction.
    # Adding them here so the dropdown + model field exist and a
    # strategy saved with one of these IDs is stable; the wiring
    # commit will flip make_strategy_bot to Strands' OpenAI adapter
    # when it sees an openai.* id.
    ModelChoice(
        id="openai.gpt-5",
        label="GPT-5 (not wired yet)",
        provider="openai",
        description="Pending OpenAI adapter wiring — selecting this will "
                    "error at bot-start until that lands.",
    ),
    ModelChoice(
        id="openai.gpt-5-mini",
        label="GPT-5 mini (not wired yet)",
        provider="openai",
        description="Pending OpenAI adapter wiring.",
    ),
)

# Maps id → ModelChoice for O(1) lookup.
_BY_ID: dict[str, ModelChoice] = {m.id: m for m in _ALLOWLIST}

# The model a strategy uses when its model_id field is empty.
# Chosen to match token_telemetry's default pricing expectations
# (Sonnet 4.6 in MODEL_PRICING).
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


def available_models() -> list[ModelChoice]:
    """Return the allowlist in display order. UI renders verbatim."""

    return list(_ALLOWLIST)


def validate_model_id(model_id: str) -> None:
    """Raise UnknownModelError if model_id is non-empty and not in
    the allowlist. Empty string is legal — it means 'use the platform
    default at resolve time'."""

    if model_id == "":
        return
    if model_id not in _BY_ID:
        msg = f"unknown model_id: {model_id!r}"
        raise UnknownModelError(msg)


def resolve_model_id(model_id: str) -> str:
    """Return the effective model ID to hand to Strands. Empty →
    DEFAULT_MODEL_ID. Unknown (should have been caught at save time)
    → raises, because silently falling back would mask a data bug."""

    if model_id == "":
        return DEFAULT_MODEL_ID
    if model_id not in _BY_ID:
        msg = f"unknown model_id at resolve time: {model_id!r}"
        raise UnknownModelError(msg)
    return model_id
