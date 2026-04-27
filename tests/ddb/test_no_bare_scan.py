"""AST guard: forbid bare `.scan(` on DDB handles outside the helper.

Three production bugs (detailed in docs/SPEC/operational_notes.md
§"TODO: DDB scan pagination sweep") stemmed from the same pattern:
a store or endpoint calling `table.scan(FilterExpression=...)` in a
single shot and trusting that the one returned page contained every
match. On this shared single-PK table it doesn't — a filter scan
returns ≤1 MB of pre-filter rows, and a light prefix family can miss
all of its matches behind a wall of MARKETDATA# rows.

The fix is the `scan_all` helper which follows LastEvaluatedKey. This
test walks `src/` and fails CI on any `.scan(` call that isn't in the
allowlist. It doesn't try to be clever about semantics — it just
flags the textual pattern so a future hurried commit can't
accidentally re-introduce the bug. If a new callsite genuinely needs
a single-shot scan (e.g. a table where pagination is proven
impossible), add an allowlist entry with a comment explaining why.

Tests files are excluded: test scaffolding scans against moto tables
with at most a handful of rows, and forcing pagination there would
just obscure the assertions.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "trading_strands"

# Files / call sites where bare `.scan(` is allowed. Each entry is
# `path::reason`; CI failure messages reference this list so a
# reviewer can see the policy.
ALLOWED: dict[str, str] = {
    # The helper itself — it's the thing everyone else calls.
    "ddb/scan_all.py":
        "The scan_all helper is the one sanctioned caller of "
        "table.scan; every other store routes through it.",
}


def _is_scan_call(node: ast.AST) -> bool:
    """True iff this AST node is a `*.scan(...)` method call."""

    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "scan"


def test_no_bare_scan_outside_helper() -> None:
    violations: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel = str(path.relative_to(SRC_ROOT))
        if rel in ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — surfaces other problems
            continue
        for node in ast.walk(tree):
            if _is_scan_call(node):
                violations.append(f"{rel}:{node.lineno}")
    assert not violations, (
        "Bare .scan(...) call outside the ddb.scan_all helper. "
        "Use trading_strands.ddb.scan_all instead — it paginates on "
        "LastEvaluatedKey. If a single-shot scan is genuinely safe "
        "(prove it in the comment), add an entry to ALLOWED in this "
        "test.\n\nViolations:\n  " + "\n  ".join(sorted(violations))
    )
