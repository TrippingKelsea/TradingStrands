"""Tests for the one-shot reconciler.

At cutover we want to bring every active strategy onto per-bot Fargate
without hand-editing each row to trigger a DDB Stream event. The
one-shot reconciler walks the strategies table, synthesizes a decision
per row, and runs them through the same `reconcile()` path the streams
handler uses — so there's exactly one code path that creates services.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.strategies_store.store import StrategyStatus, StrategyStore
from trading_strands.supervisor.reconcile_all import (
    ReconcileAllSummary,
    decisions_from_store,
    reconcile_all,
)
from trading_strands.supervisor.strategy_supervisor import Action


def _make_table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


class FakeECS:
    """Records service/task-def state — mirrors the one in
    test_strategy_supervisor.py (kept separate to avoid cross-test
    coupling on the fake's API)."""

    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {}
        self.task_defs: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def register_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"register_td:{kwargs.get('family', '?')}")
        revision = len(self.task_defs) + 1
        arn = f"arn:aws:ecs:test:0:task-definition/{kwargs['family']}:{revision}"
        td = {"taskDefinitionArn": arn, **kwargs}
        self.task_defs.append(td)
        return {"taskDefinition": td}

    def describe_services(
        self, cluster: str, services: list[str],
    ) -> dict[str, Any]:
        self.calls.append(f"describe:{services[0]}")
        s = self.services.get(services[0])
        if s is None:
            return {"services": [{"status": "MISSING"}]}
        return {"services": [s]}

    def create_service(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(f"create:{kwargs['serviceName']}")
        self.services[kwargs["serviceName"]] = {
            "serviceName": kwargs["serviceName"],
            "status": "ACTIVE",
            "desiredCount": kwargs.get("desiredCount", 1),
        }
        return {"service": self.services[kwargs["serviceName"]]}

    def update_service(
        self, cluster: str, service: str, desiredCount: int,
    ) -> dict[str, Any]:
        self.calls.append(f"update:{service}:{desiredCount}")
        if service in self.services:
            self.services[service]["desiredCount"] = desiredCount
        return {"service": self.services.get(service, {})}

    def delete_service(
        self, cluster: str, service: str, force: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(f"delete:{service}")
        self.services.pop(service, None)
        return {}


def _ecs_params() -> dict[str, Any]:
    return {
        "cluster": "ts-cluster",
        "base_task_definition_family": "ts-bot",
        "container_image": "0.dkr.ecr.test.amazonaws.com/ts:latest",
        "task_role_arn": "arn:aws:iam::0:role/ts-task",
        "execution_role_arn": "arn:aws:iam::0:role/ts-exec",
        "subnet_ids": ["subnet-a"],
        "security_group_ids": ["sg-1"],
        "log_group_name": "/ts/bots",
        "region": "us-west-2",
        "extra_env": {"DYNAMODB_TABLE": "t"},
    }


# ── decisions_from_store ─────────────────────────────────────────────


def test_decisions_only_for_active_strategies() -> None:
    """Paused and stopped strategies don't need services — the cutover
    should not start services for strategies the user has chosen to
    leave dormant."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        active = store.create(
            org_id="o1", author_user_id="u1",
            name="live", markdown="x",
        )
        paused = store.create(
            org_id="o1", author_user_id="u1",
            name="paused", markdown="x",
        )
        store.update(paused.strategy_id, {"status": StrategyStatus.PAUSED.value})
        stopped = store.create(
            org_id="o1", author_user_id="u1",
            name="stopped", markdown="x",
        )
        store.update(stopped.strategy_id, {"status": StrategyStatus.STOPPED.value})

        decisions = decisions_from_store(store)
        assert len(decisions) == 1
        assert decisions[0].action is Action.ENSURE_RUNNING
        assert decisions[0].strategy_id == active.strategy_id
        assert decisions[0].org_id == "o1"


def test_decisions_empty_table_returns_empty() -> None:
    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        assert decisions_from_store(store) == []


def test_decisions_skip_rows_without_org_id() -> None:
    """Legacy pre-refactor strategies without org_id can't be turned
    into services — we need ORG_ID on the task. Skip them cleanly;
    they'd have been pruned by bootstrap anyway."""

    with mock_aws():
        table = _make_table()
        # Bypass StrategyStore.create (which requires org_id) and write
        # a raw row to simulate legacy state.
        table.put_item(Item={
            "pk": "STRATEGY#legacy",
            "strategy_id": "legacy",
            "org_id": "",  # empty
            "author_user_id": "u1",
            "name": "legacy",
            "markdown": "x",
            "status": "active",
            "created_at": 1,
            "updated_at": 1,
        })
        store = StrategyStore(table)
        assert decisions_from_store(store) == []


# ── reconcile_all ────────────────────────────────────────────────────


def test_reconcile_all_creates_service_for_each_active() -> None:
    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        a = store.create(org_id="o1", author_user_id="u1", name="a", markdown="x")
        b = store.create(org_id="o2", author_user_id="u2", name="b", markdown="x")
        ecs = FakeECS()

        summary = reconcile_all(
            store=store, ecs_client=ecs, dry_run=False, **_ecs_params(),
        )

        assert isinstance(summary, ReconcileAllSummary)
        assert summary.total == 2
        assert summary.reconciled == 2
        assert summary.errors == 0
        assert f"ts-strategy-{a.strategy_id}" in ecs.services
        assert f"ts-strategy-{b.strategy_id}" in ecs.services


def test_reconcile_all_dry_run_makes_no_ecs_changes() -> None:
    """Operators need a safe way to preview what cutover would do
    before firing it. dry_run must not touch ECS at all."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        store.create(org_id="o1", author_user_id="u1", name="a", markdown="x")
        ecs = FakeECS()

        summary = reconcile_all(
            store=store, ecs_client=ecs, dry_run=True, **_ecs_params(),
        )

        assert summary.total == 1
        assert summary.reconciled == 0
        assert summary.dry_run_actions == ["ENSURE_RUNNING"]
        assert ecs.calls == []


def test_reconcile_all_continues_on_per_strategy_failure() -> None:
    """A bad strategy row (e.g. capacity error from ECS) must not stop
    the rest of the fleet from being reconciled. Errors are counted
    and summarized, not raised."""

    class FlakyECS(FakeECS):
        def create_service(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["serviceName"].endswith("broken"):
                raise RuntimeError("capacity error")
            return super().create_service(**kwargs)

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        # Give one strategy an ID ending "broken" to match the fake.
        # Since StrategyStore generates random IDs, we write a raw row.
        table.put_item(Item={
            "pk": "STRATEGY#broken",
            "strategy_id": "broken", "org_id": "o1",
            "author_user_id": "u1", "name": "x", "markdown": "y",
            "status": "active", "symbols": ["AAPL"],
            "capital": "100", "created_at": 1, "updated_at": 1,
        })
        good = store.create(org_id="o1", author_user_id="u1", name="g", markdown="x")
        ecs = FlakyECS()

        summary = reconcile_all(
            store=store, ecs_client=ecs, dry_run=False, **_ecs_params(),
        )

        assert summary.total == 2
        assert summary.reconciled == 1
        assert summary.errors == 1
        assert f"ts-strategy-{good.strategy_id}" in ecs.services
        assert "ts-strategy-broken" not in ecs.services
