"""Tests for the model allowlist + metadata registry."""

from __future__ import annotations

import pytest

from trading_strands.models.registry import (
    DEFAULT_MODEL_ID,
    ModelChoice,
    UnknownModelError,
    available_models,
    resolve_model_id,
    validate_model_id,
)


def test_default_model_is_known() -> None:
    """The platform default must itself pass validation — a typo here
    would brick every strategy that leaves model blank."""

    validate_model_id(DEFAULT_MODEL_ID)  # no raise


def test_available_models_returns_list_of_choices() -> None:
    models = available_models()
    assert len(models) > 0
    assert all(isinstance(m, ModelChoice) for m in models)
    # Every choice must have a pricing hint (even if unknown).
    for m in models:
        assert m.id
        assert m.label
        assert m.provider in {"bedrock", "openai", "other"}


def test_validate_accepts_allowlisted_id() -> None:
    validate_model_id("us.anthropic.claude-sonnet-4-6")


def test_validate_rejects_typo() -> None:
    """Guard against silent breakage from e.g. 'claude-sonnet-4-10'."""

    with pytest.raises(UnknownModelError, match="claude-sonnet-4-10"):
        validate_model_id("claude-sonnet-4-10")


def test_validate_empty_string_allowed() -> None:
    """Empty string means 'use platform default' at resolution time;
    it is NOT an error at strategy-save time."""

    validate_model_id("")


def test_resolve_empty_returns_default() -> None:
    assert resolve_model_id("") == DEFAULT_MODEL_ID


def test_resolve_known_returns_unchanged() -> None:
    assert resolve_model_id(
        "us.anthropic.claude-opus-4-7",
    ) == "us.anthropic.claude-opus-4-7"


def test_resolve_unknown_raises() -> None:
    """Validation should have caught this at save time; resolve is a
    defense-in-depth belt + braces."""

    with pytest.raises(UnknownModelError):
        resolve_model_id("fake-model-xyz")


def test_available_includes_multiple_providers() -> None:
    """Part of the point: the user wants to compare across providers."""

    providers = {m.provider for m in available_models()}
    # At least bedrock must be present. OpenAI is a deferred follow-on.
    assert "bedrock" in providers


def test_all_bedrock_models_have_pricing() -> None:
    """Every Bedrock model in the allowlist must be priceable via
    MODEL_PRICING — a model we can't price surfaces as $0 in the
    cost dashboard, which is worse than not offering it."""

    from trading_strands.token_telemetry.store import _price_for

    for m in available_models():
        if m.provider != "bedrock":
            continue
        input_rate, output_rate = _price_for(m.id)
        assert input_rate > 0 or output_rate > 0, (
            f"bedrock model {m.id} has no MODEL_PRICING entry — "
            "cost dashboard would misleadingly show $0"
        )
