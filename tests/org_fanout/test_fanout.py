"""Tests for the org-fanout Lambda.

Analog of BotProvisioner, but for per-org review agents. Walks the
tenancy store, invokes the target review-agent function once per
org. The EventBridge rule supplies the target function name in the
event payload — one Lambda can fan out Risk, Compliance, or Auditor
depending on who invokes it.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.org_fanout.fanout import (
    enumerate_orgs,
    fan_out_review,
)
from trading_strands.tenancy.store import OrgType, TenancyStore


def _make_table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── enumerate_orgs ───────────────────────────────────────────────────


def test_enumerate_returns_all_orgs_including_system() -> None:
    """System org matters too — sysadmin-run strategies exist there
    and need the same review coverage as customer orgs."""

    with mock_aws():
        table = _make_table()
        store = TenancyStore(table)
        sys_org = store.create_org("System", OrgType.SYSTEM)
        cust_a = store.create_org("Customer A")
        cust_b = store.create_org("Customer B")

        org_ids = enumerate_orgs(store)
        assert set(org_ids) == {sys_org.org_id, cust_a.org_id, cust_b.org_id}


def test_enumerate_empty_table() -> None:
    with mock_aws():
        table = _make_table()
        store = TenancyStore(table)
        assert enumerate_orgs(store) == []


# ── fan_out_review ───────────────────────────────────────────────────


class FakeLambdaClient:
    def __init__(self, fail_orgs: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail_orgs or set()

    def invoke(
        self, FunctionName: str, InvocationType: str, Payload: bytes,
    ) -> dict[str, Any]:
        import json
        payload = json.loads(Payload)
        self.calls.append((FunctionName, payload))
        if payload.get("org_id") in self._fail:
            raise RuntimeError(f"boom: {payload['org_id']}")
        return {"StatusCode": 202}


def test_fan_out_invokes_once_per_org() -> None:
    client = FakeLambdaClient()
    results = fan_out_review(
        lambda_client=client,
        target_function="trading-strands-risk-agent",
        org_ids=["org-1", "org-2", "org-3"],
    )
    assert len(client.calls) == 3
    assert all(fn == "trading-strands-risk-agent" for fn, _ in client.calls)
    assert {c[1]["org_id"] for c in client.calls} == {"org-1", "org-2", "org-3"}
    assert all(r.ok for r in results)
    assert [r.org_id for r in results] == ["org-1", "org-2", "org-3"]


def test_fan_out_async_invocation_type() -> None:
    """Must be Event (async). One slow org must not block others."""

    class AssertingClient:
        def __init__(self) -> None:
            self.types: list[str] = []

        def invoke(
            self, FunctionName: str, InvocationType: str, Payload: bytes,
        ) -> dict[str, Any]:
            self.types.append(InvocationType)
            return {"StatusCode": 202}

    client = AssertingClient()
    fan_out_review(
        lambda_client=client,
        target_function="trading-strands-risk-agent",
        org_ids=["o1"],
    )
    assert client.types == ["Event"]


def test_fan_out_continues_on_per_org_failure() -> None:
    client = FakeLambdaClient(fail_orgs={"org-2"})
    results = fan_out_review(
        lambda_client=client,
        target_function="trading-strands-risk-agent",
        org_ids=["org-1", "org-2", "org-3"],
    )
    assert [r.ok for r in results] == [True, False, True]
    assert "boom" in (results[1].error or "")
    assert len(client.calls) == 3


def test_fan_out_empty_org_list_is_noop() -> None:
    client = FakeLambdaClient()
    assert fan_out_review(
        lambda_client=client,
        target_function="trading-strands-risk-agent",
        org_ids=[],
    ) == []
    assert client.calls == []


# ── handler ──────────────────────────────────────────────────────────


def test_handler_requires_target_function() -> None:
    """Invocation without a target function is a misconfigured schedule —
    fail loudly rather than silently invoke nothing."""

    from trading_strands.org_fanout import fanout

    with mock_aws():
        table = _make_table()
        store = TenancyStore(table)
        store.create_org("A")

        result = fanout._run(
            store=store,
            lambda_client=FakeLambdaClient(),
            event={},  # no target_function
        )
        assert result["ok"] is False
        assert "target_function" in result["error"]


def test_handler_reads_target_from_event() -> None:
    from trading_strands.org_fanout import fanout

    with mock_aws():
        table = _make_table()
        store = TenancyStore(table)
        org_a = store.create_org("A")
        org_b = store.create_org("B")

        client = FakeLambdaClient()
        result = fanout._run(
            store=store,
            lambda_client=client,
            event={"target_function": "trading-strands-compliance-agent"},
        )
        assert result["ok"] is True
        assert result["total"] == 2
        assert result["succeeded"] == 2
        assert {c[1]["org_id"] for c in client.calls} == {
            org_a.org_id, org_b.org_id,
        }
        assert all(
            c[0] == "trading-strands-compliance-agent" for c in client.calls
        )


def test_handler_reports_partial_failure() -> None:
    from trading_strands.org_fanout import fanout

    with mock_aws():
        table = _make_table()
        store = TenancyStore(table)
        a = store.create_org("A")
        b = store.create_org("B")

        client = FakeLambdaClient(fail_orgs={a.org_id})
        result = fanout._run(
            store=store,
            lambda_client=client,
            event={"target_function": "trading-strands-auditor-agent"},
        )
        assert result["ok"] is False  # any failure flips ok
        assert result["total"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        # The passing org got invoked too — failures don't short-circuit.
        assert any(c[1]["org_id"] == b.org_id for c in client.calls)
