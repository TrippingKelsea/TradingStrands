"""Tests for RecommendationsStore.

Recommendations are written by each review agent (Risk, Compliance,
Auditor) AND aggregated per-org for the dashboard's Org Advisories
endpoint. Agent-memory recommendations.md remains the audit trail;
this store is the live view.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.recommendations_store.store import (
    RecommendationSeverity,
    RecommendationsStore,
)


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def test_append_and_list_roundtrip() -> None:
    with mock_aws():
        store = RecommendationsStore(_table())
        store.append(
            org_id="org-a", agent_type="risk", agent_id="agent-1",
            severity="warn", summary="concentration rising",
            body="detail", created_at=1_700_000_000,
        )
        [entry] = store.list_for_org("org-a")
        assert entry.severity is RecommendationSeverity.WARN
        assert entry.summary == "concentration rising"
        assert entry.agent_type == "risk"
        assert entry.created_at == 1_700_000_000


def test_list_for_org_is_org_scoped() -> None:
    """Privacy invariant: other orgs' advisories must not appear in
    another org's list. The DDB scan filter rules out cross-org items
    at the table layer."""

    with mock_aws():
        store = RecommendationsStore(_table())
        store.append(
            org_id="org-a", agent_type="risk", agent_id="r",
            severity="info", summary="for A", created_at=1,
        )
        store.append(
            org_id="org-b", agent_type="risk", agent_id="r",
            severity="info", summary="for B", created_at=2,
        )
        a = store.list_for_org("org-a")
        b = store.list_for_org("org-b")
        assert [e.summary for e in a] == ["for A"]
        assert [e.summary for e in b] == ["for B"]


def test_list_returns_newest_first() -> None:
    with mock_aws():
        store = RecommendationsStore(_table())
        for ts in [100, 300, 200]:
            store.append(
                org_id="org-x", agent_type="risk", agent_id="r",
                severity="info", summary=f"t{ts}", created_at=ts,
            )
        entries = store.list_for_org("org-x")
        assert [e.summary for e in entries] == ["t300", "t200", "t100"]


def test_list_limit_applied() -> None:
    with mock_aws():
        store = RecommendationsStore(_table())
        for i in range(10):
            store.append(
                org_id="o", agent_type="risk", agent_id="r",
                severity="info", summary=f"s{i}", created_at=100 + i,
            )
        entries = store.list_for_org("o", limit=3)
        assert len(entries) == 3
        assert entries[0].summary == "s9"


def test_ttl_written_on_every_entry() -> None:
    """The TTL attribute is required so DDB self-prunes old advisories
    after the retention window — without it, the table grows forever."""

    with mock_aws():
        table = _table()
        store = RecommendationsStore(table)
        store.append(
            org_id="o", agent_type="risk", agent_id="r",
            severity="warn", summary="s", created_at=1_700_000_000,
        )
        resp = table.scan()
        items = resp["Items"]
        assert len(items) == 1
        assert int(items[0]["ttl"]) > 1_700_000_000


def test_severity_string_accepted() -> None:
    """Callers pass severity as either an enum or a string — makes
    the JSON-ingested LLM output path easier."""

    with mock_aws():
        store = RecommendationsStore(_table())
        entry = store.append(
            org_id="o", agent_type="risk", agent_id="r",
            severity="critical", summary="s", created_at=1,
        )
        assert entry.severity is RecommendationSeverity.CRITICAL
