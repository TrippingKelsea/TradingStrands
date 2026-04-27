"""Model registry: allowlisted model IDs a strategy can select.

An allowlist (not free-text) means typos in model IDs fail at
strategy-save time instead of silently at bot-start or — worse —
showing $0 cost because pricing isn't registered. See tests for
the invariants this module enforces.
"""

from trading_strands.models.registry import (
    DEFAULT_MODEL_ID,
    ModelChoice,
    UnknownModelError,
    available_models,
    resolve_model_id,
    validate_model_id,
)

__all__ = [
    "DEFAULT_MODEL_ID",
    "ModelChoice",
    "UnknownModelError",
    "available_models",
    "resolve_model_id",
    "validate_model_id",
]
