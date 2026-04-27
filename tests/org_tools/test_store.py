"""Tests for OrgToolsStore — per-org tool availability gate.

Per SPEC §5.4: org-level control is a gate (orgadmin can disable
a tool, which takes it away from strategies that had it enabled).
Absence is the default-deny state for availability — orgadmin
flipping to enabled=true makes the tool available to strategy
opt-in.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.org_tools.store import OrgToolsStore


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def test_tool_disabled_by_default() -> None:
    """Absent row → not enabled."""

    with mock_aws():
        store = OrgToolsStore(_table())
        assert store.is_enabled("o1", "news") is False


def test_set_and_read_enabled() -> None:
    with mock_aws():
        store = OrgToolsStore(_table())
        store.set_enabled("o1", "news", enabled=True, updated_by="u1")
        assert store.is_enabled("o1", "news") is True


def test_set_to_disabled_after_enabled() -> None:
    """Orgadmin flips from enabled back to disabled — strategies
    that had the tool enabled get it removed on next bot restart."""

    with mock_aws():
        store = OrgToolsStore(_table())
        store.set_enabled("o1", "news", enabled=True, updated_by="u1")
        store.set_enabled("o1", "news", enabled=False, updated_by="u1")
        assert store.is_enabled("o1", "news") is False


def test_list_for_org_returns_configured_tools() -> None:
    with mock_aws():
        store = OrgToolsStore(_table())
        store.set_enabled("o1", "news", enabled=True, updated_by="u1")
        store.set_enabled("o1", "filings", enabled=False, updated_by="u1")
        # Other org unrelated.
        store.set_enabled("o2", "news", enabled=True, updated_by="u2")

        configured = store.list_for_org("o1")
        names = {c.tool_name for c in configured}
        assert names == {"news", "filings"}
        by_name = {c.tool_name: c for c in configured}
        assert by_name["news"].enabled is True
        assert by_name["filings"].enabled is False


def test_list_for_org_empty_when_no_config() -> None:
    with mock_aws():
        store = OrgToolsStore(_table())
        assert store.list_for_org("o1") == []


def test_isolation_per_org() -> None:
    """o1 enabling news doesn't implicitly enable for o2."""

    with mock_aws():
        store = OrgToolsStore(_table())
        store.set_enabled("o1", "news", enabled=True, updated_by="u1")
        assert store.is_enabled("o2", "news") is False
