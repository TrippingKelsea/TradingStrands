"""Tests for the BotProvisioner enumeration + fan-out Lambda.

The provisioner's job is to walk the strategies table, pick out the
active bots, and invoke the Self-Critique Lambda once per bot. It does
NOT run the reflection itself — that's the Self-Critique Lambda's job.
Keeping the two concerns separate means retries, timeouts, and per-bot
failures don't block the rest of the fleet.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.provisioner.bot_provisioner import (
    InvocationResult,
    enumerate_active_bots,
    fan_out_self_critique,
)
from trading_strands.strategies_store.store import StrategyStatus, StrategyStore


def _make_table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


# ── enumerate_active_bots ────────────────────────────────────────────


def test_enumerate_returns_only_active_strategies() -> None:
    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        active = store.create(
            org_id="o1", author_user_id="u1",
            name="active-strat", markdown="rules",
        )
        paused = store.create(
            org_id="o1", author_user_id="u1",
            name="paused-strat", markdown="rules",
        )
        store.update(paused.strategy_id, {"status": StrategyStatus.PAUSED.value})
        stopped = store.create(
            org_id="o2", author_user_id="u2",
            name="stopped-strat", markdown="rules",
        )
        store.update(stopped.strategy_id, {"status": StrategyStatus.STOPPED.value})

        bots = enumerate_active_bots(store)

        # Only the active strategy should appear.
        assert len(bots) == 1
        org_id, bot_id = bots[0]
        assert org_id == "o1"
        assert bot_id == f"strategy-{active.strategy_id}"


def test_enumerate_handles_empty_table() -> None:
    """No strategies: return empty list, don't raise."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        assert enumerate_active_bots(store) == []


def test_enumerate_spans_multiple_orgs() -> None:
    """Bots from every org must appear — this is a system-wide fan-out."""

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        store.create(org_id="o1", author_user_id="u1", name="s1", markdown="x")
        store.create(org_id="o2", author_user_id="u2", name="s2", markdown="x")
        store.create(org_id="o3", author_user_id="u3", name="s3", markdown="x")

        bots = enumerate_active_bots(store)

        assert {org for org, _ in bots} == {"o1", "o2", "o3"}


# ── fan_out_self_critique ────────────────────────────────────────────


class FakeLambdaClient:
    """Stand-in for boto3 lambda client. Records calls so we can assert."""

    def __init__(self, fail_bots: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_bots = fail_bots or set()

    def invoke(
        self, FunctionName: str, InvocationType: str, Payload: bytes,
    ) -> dict[str, Any]:
        import json
        payload = json.loads(Payload)
        self.calls.append((FunctionName, payload))
        # self-critique uses `bot_id`, memory_compactor uses `agent_id`.
        # Accept either for failure injection.
        who = payload.get("bot_id") or payload.get("agent_id", "")
        if who in self._fail_bots:
            raise RuntimeError(f"boom: {who}")
        # Lambda returns an HTTP-style response envelope.
        return {"StatusCode": 202}  # async invoke = 202


def test_fan_out_invokes_once_per_bot() -> None:
    client = FakeLambdaClient()
    results = fan_out_self_critique(
        lambda_client=client,
        function_name="self-critique",
        bots=[("o1", "strategy-a"), ("o2", "strategy-b")],
    )

    assert len(client.calls) == 2
    assert client.calls[0][0] == "self-critique"
    assert client.calls[0][1] == {"org_id": "o1", "bot_id": "strategy-a"}
    assert client.calls[1][1] == {"org_id": "o2", "bot_id": "strategy-b"}

    assert all(isinstance(r, InvocationResult) for r in results)
    assert all(r.ok for r in results)
    assert [r.bot_id for r in results] == ["strategy-a", "strategy-b"]


def test_fan_out_uses_async_invocation_type() -> None:
    """Async so one slow bot doesn't block the rest of the fan-out."""

    class AssertingClient:
        def __init__(self) -> None:
            self.invocation_types: list[str] = []

        def invoke(
            self, FunctionName: str, InvocationType: str, Payload: bytes,
        ) -> dict[str, Any]:
            self.invocation_types.append(InvocationType)
            return {"StatusCode": 202}

    client = AssertingClient()
    fan_out_self_critique(
        lambda_client=client,
        function_name="self-critique",
        bots=[("o1", "b1")],
    )
    assert client.invocation_types == ["Event"]


def test_fan_out_continues_on_per_bot_failure() -> None:
    """One bot's failure must not stop the rest — we report it in the
    result list and move on."""

    client = FakeLambdaClient(fail_bots={"strategy-b"})
    results = fan_out_self_critique(
        lambda_client=client,
        function_name="self-critique",
        bots=[("o1", "strategy-a"), ("o2", "strategy-b"), ("o3", "strategy-c")],
    )

    assert [r.ok for r in results] == [True, False, True]
    assert "boom" in (results[1].error or "")
    # The failing invoke was still attempted, as were the ones after it.
    assert len(client.calls) == 3


def test_fan_out_with_empty_bot_list() -> None:
    """Nothing to fan out: return empty list, don't call invoke."""

    client = FakeLambdaClient()
    assert fan_out_self_critique(
        lambda_client=client,
        function_name="self-critique",
        bots=[],
    ) == []
    assert client.calls == []


# ── handler ──────────────────────────────────────────────────────────


def test_handler_enumerates_and_invokes() -> None:
    """End-to-end: handler pulls strategies from DDB and invokes the
    Self-Critique function for each active bot."""

    from trading_strands.provisioner import bot_provisioner

    with mock_aws():
        table = _make_table()
        store = StrategyStore(table)
        active = store.create(
            org_id="o1", author_user_id="u1",
            name="active", markdown="rules",
        )
        paused = store.create(
            org_id="o1", author_user_id="u1",
            name="paused", markdown="rules",
        )
        store.update(paused.strategy_id, {"status": StrategyStatus.PAUSED.value})

        client = FakeLambdaClient()

        # Dependency-injection seams: inject the table + lambda client
        # rather than touching boto3 globals inside the handler.
        result = bot_provisioner._run(
            table=table,
            lambda_client=client,
            function_name="self-critique",
        )

        assert result["ok"] is True
        assert result["total"] == 1
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        assert client.calls == [
            ("self-critique", {"org_id": "o1", "bot_id": f"strategy-{active.strategy_id}"}),
        ]


def test_run_target_memory_compactor_fans_out_compactor_payload() -> None:
    """target=memory_compactor switches the fan-out payload shape so
    the same provisioner can drive both the weekend self-critique and
    the nightly memory compactor. Compactor payload is
    (org_id, agent_type=strategy, agent_id=<bot_id>) per the compactor
    Lambda's event contract."""

    from unittest.mock import patch

    import boto3
    from moto import mock_aws

    from trading_strands.provisioner import bot_provisioner as bp
    from trading_strands.strategies_store.store import StrategyStore

    with mock_aws(), patch.dict(
        os.environ,
        {
            "DYNAMODB_TABLE": "t",
            "SELF_CRITIQUE_FUNCTION_NAME": "sc",
            "MEMORY_COMPACTOR_FUNCTION_NAME": "mc",
        },
    ):
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(
            TableName="t",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("t")
        store = StrategyStore(table)
        strat = store.create(
            org_id="o1", author_user_id="u1",
            name="active", markdown="rules",
        )
        client = FakeLambdaClient()
        result = bp._run(
            table=table, lambda_client=client,
            function_name="mc", target="memory_compactor",
        )
        assert result["ok"] is True
        assert result["target"] == "memory_compactor"
        assert client.calls == [
            ("mc", {
                "org_id": "o1",
                "agent_type": "strategy",
                "agent_id": f"strategy-{strat.strategy_id}",
            }),
        ]
