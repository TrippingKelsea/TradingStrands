"""Exhaustive DDB table scan that follows LastEvaluatedKey.

Every callsite that applies a FilterExpression on a shared single-PK
table MUST use this helper instead of `table.scan(...)` directly.
The reason, in one sentence: DDB scan returns at most ~1 MB of
*pre-filter* items per call, so a one-shot filtered scan against a
table dominated by one prefix family (in this codebase: MARKETDATA#)
can burn its 1 MB budget on non-matching rows and return zero
matches even when matches exist. The fix is always
LastEvaluatedKey follow-up.

Three production bugs have stemmed from this class:
  - StrategyStore.list_all skipped an ACTIVE strategy in reconcile
  - TenancyStore.memberships_for_user returned [] for a real member
    → cascaded into 403 on every org-scoped endpoint
  - OrgToolsStore.list_for_org dropped configured tools → admin
    toggle checkbox reverted instantly on refresh

Each was "fixed" inline before this module landed; this module
promotes the pattern to a shared helper so future stores can't ship
the bug again by copying the wrong pattern. A pytest AST check
(tests/ddb/test_no_bare_scan.py) forbids `.scan(` on a table handle
outside this file.

Runtime signal: when the first page of a scan sets LastEvaluatedKey,
that's a "this scan would have silently dropped rows if a naive
caller had ignored the continuation" event. Logged at WARN via
structlog so CloudWatch can alarm on chronically-paginating scans
(usually a hint to migrate the access pattern off scan entirely).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


def scan_all(table: Any, filter_expression: Any) -> list[dict[str, Any]]:
    """Filter-scan `table`, following LastEvaluatedKey to exhaustion.

    Returns the concatenated list of items across pages. Callers are
    responsible for any further filtering or typed mapping on the
    returned dicts — this helper stays at the dict level so it can
    serve every store's row shape.
    """

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"FilterExpression": filter_expression}
    paginated = False
    page_count = 0
    while True:
        resp = table.scan(**kwargs)
        page_count += 1
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        paginated = True
        kwargs["ExclusiveStartKey"] = last
    if paginated:
        # Surface chronic pagination; one WARN per call is a
        # manageable signal. If a caller is chewing through many
        # pages every tick, the access pattern is wrong (likely a
        # missing GSI or table-sharding opportunity).
        logger.warning(
            "ddb.scan_all.paginated",
            pages=page_count, row_count=len(items),
        )
    return items
