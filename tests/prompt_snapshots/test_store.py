"""Tests for PromptSnapshotStore.

The store holds one row per bot, overwritten each tick — writes are
idempotent and the latest prompt always wins. Missing rows return
None (not an error) so the endpoint can render a placeholder for a
bot that hasn't decided yet.
"""

from __future__ import annotations

from typing import Any

import boto3
from moto import mock_aws

from trading_strands.prompt_snapshots.store import PromptSnapshotStore


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def test_write_then_get_roundtrip() -> None:
    with mock_aws():
        store = PromptSnapshotStore(_table())
        written = store.write(
            bot_id="strategy-abc",
            org_id="org-1",
            system_prompt="You are a bot.",
            user_prompt="Price is $100.",
            tick=7,
        )
        got = store.get("strategy-abc")
        assert got is not None
        assert got.bot_id == "strategy-abc"
        assert got.org_id == "org-1"
        assert got.system_prompt == "You are a bot."
        assert got.user_prompt == "Price is $100."
        assert got.tick == 7
        assert got.rendered_at == written.rendered_at
        assert got.rendered_at > 0


def test_write_overwrites_previous_snapshot() -> None:
    """Single-row-per-bot invariant — each tick replaces the prior
    snapshot. The audit trail lives in the memory file."""

    with mock_aws():
        store = PromptSnapshotStore(_table())
        store.write(
            bot_id="s1", org_id="o",
            system_prompt="v1", user_prompt="u1", tick=1,
        )
        store.write(
            bot_id="s1", org_id="o",
            system_prompt="v2", user_prompt="u2", tick=2,
        )
        got = store.get("s1")
        assert got is not None
        assert got.tick == 2
        assert got.user_prompt == "u2"


def test_get_missing_returns_none() -> None:
    """Bots that haven't decided yet have no snapshot. None, not
    exception — the endpoint renders a placeholder rather than 500."""

    with mock_aws():
        store = PromptSnapshotStore(_table())
        assert store.get("never-written") is None


def test_snapshots_are_per_bot_isolated() -> None:
    with mock_aws():
        store = PromptSnapshotStore(_table())
        store.write(
            bot_id="bot-a", org_id="o",
            system_prompt="A", user_prompt="uA", tick=1,
        )
        store.write(
            bot_id="bot-b", org_id="o",
            system_prompt="B", user_prompt="uB", tick=1,
        )
        a = store.get("bot-a")
        b = store.get("bot-b")
        assert a is not None and a.system_prompt == "A"
        assert b is not None and b.system_prompt == "B"
