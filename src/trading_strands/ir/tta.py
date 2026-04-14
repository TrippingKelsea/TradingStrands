"""TTA predicate evaluation (§4.1).

Threshold-to-action predicates are JSON-expressible predicate trees
evaluated deterministically by the orchestrator. They define when a
strategy bot should be woken for a decision.

Predicate forms:
  Comparison: {"field": "price.AAPL", "op": "gt", "value": 150}
  And:        {"and": [pred, pred, ...]}
  Or:         {"or":  [pred, pred, ...]}
  Not:        {"not": pred}
  Cross:      {"cross": "above"|"below", "field": "...", "value": N}
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# A predicate is a JSON-expressible dict. Permissive for now (§10.2).
Predicate = dict[str, Any]

# Context is a flat dict of field names to Decimal values.
Context = dict[str, Decimal]

_OPS = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


def evaluate(
    pred: Predicate,
    ctx: Context,
    prev_ctx: Context | None = None,
) -> bool:
    """Evaluate a TTA predicate against the current (and optionally previous) context.

    This is deterministic code in the orchestrator hot path — no LLM calls.
    """
    # Logical operators
    if "and" in pred:
        return all(evaluate(p, ctx, prev_ctx) for p in pred["and"])
    if "or" in pred:
        return any(evaluate(p, ctx, prev_ctx) for p in pred["or"])
    if "not" in pred:
        return not evaluate(pred["not"], ctx, prev_ctx)

    # Cross predicates (require previous context)
    if "cross" in pred:
        return _eval_cross(pred, ctx, prev_ctx)

    # Comparison predicates
    if "field" in pred and "op" in pred:
        return _eval_comparison(pred, ctx)

    msg = f"invalid predicate: {pred}"
    raise ValueError(msg)


def _eval_comparison(pred: Predicate, ctx: Context) -> bool:
    field: str = pred["field"]
    op: str = pred["op"]
    value = Decimal(str(pred["value"]))

    if op not in _OPS:
        msg = f"unknown operator: {op}"
        raise ValueError(msg)

    actual = ctx.get(field)
    if actual is None:
        return False

    return _OPS[op](actual, value)  # type: ignore[no-any-return]


def _eval_cross(
    pred: Predicate,
    ctx: Context,
    prev_ctx: Context | None,
) -> bool:
    if prev_ctx is None:
        return False

    direction: str = pred["cross"]
    field: str = pred["field"]
    threshold = Decimal(str(pred["value"]))

    current = ctx.get(field)
    previous = prev_ctx.get(field)

    if current is None or previous is None:
        return False

    if direction == "above":
        return previous <= threshold < current
    if direction == "below":
        return previous >= threshold > current

    msg = f"unknown cross direction: {direction}"
    raise ValueError(msg)
