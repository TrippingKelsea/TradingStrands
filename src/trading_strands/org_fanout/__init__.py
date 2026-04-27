"""Org-fanout Lambda.

Enumerates every org in the tenancy store and invokes a target
review-agent Lambda once per org, asynchronously. One shared Lambda
for Risk / Compliance / Auditor — the EventBridge rule passes the
target function name in the event payload.
"""

from trading_strands.org_fanout.fanout import (
    InvocationResult,
    enumerate_orgs,
    fan_out_review,
    handler,
)

__all__ = [
    "InvocationResult",
    "enumerate_orgs",
    "fan_out_review",
    "handler",
]
