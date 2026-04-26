"""Tests for single-bot mode selection in app.py.

Single-bot mode is triggered by STRATEGY_ID being set in the env. In
that mode the process loads exactly one strategy from DDB and does not
poll for others. The StrategySupervisor Lambda starts/stops these
per-bot Fargate tasks based on strategy status changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_strands.app import (
    SingleBotConfig,
    load_single_bot_config,
    single_bot_mode_enabled,
)


def test_single_bot_mode_disabled_by_default() -> None:
    assert single_bot_mode_enabled({}) is False
    assert single_bot_mode_enabled({"DYNAMODB_TABLE": "t"}) is False


def test_single_bot_mode_enabled_when_strategy_id_set() -> None:
    assert single_bot_mode_enabled({"STRATEGY_ID": "abc"}) is True


def test_single_bot_mode_requires_org_id() -> None:
    """STRATEGY_ID without ORG_ID is a misconfiguration — raise loudly."""

    class FakeTable:
        def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("should not be called")

    with pytest.raises(RuntimeError, match="ORG_ID"):
        load_single_bot_config(
            FakeTable(),
            env={"STRATEGY_ID": "abc"},  # missing ORG_ID
        )


def test_load_single_bot_config_reads_strategy_from_ddb() -> None:
    """Happy path: STRATEGY_ID + ORG_ID → loads strategy markdown +
    symbols + capital + name from DDB."""

    class FakeTable:
        def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
            assert Key == {"pk": "STRATEGY#abc"}
            return {
                "Item": {
                    "pk": "STRATEGY#abc",
                    "strategy_id": "abc",
                    "org_id": "org-1",
                    "author_user_id": "u1",
                    "name": "Momentum",
                    "markdown": "## Rules\n- buy on breakout",
                    "symbols": ["AAPL", "MSFT"],
                    "capital": "5000",
                    "status": "active",
                    "created_at": 1,
                    "updated_at": 1,
                },
            }

    cfg = load_single_bot_config(
        FakeTable(),
        env={"STRATEGY_ID": "abc", "ORG_ID": "org-1"},
    )
    assert isinstance(cfg, SingleBotConfig)
    assert cfg.strategy_id == "abc"
    assert cfg.org_id == "org-1"
    assert cfg.bot_id == "strategy-abc"
    assert cfg.symbols == ["AAPL", "MSFT"]
    assert str(cfg.capital) == "5000"
    assert "buy on breakout" in cfg.strategy_prompt


def test_load_single_bot_config_rejects_cross_org_claim() -> None:
    """If ORG_ID in env doesn't match the strategy's stored org_id, the
    task has been started against the wrong strategy — fail closed.
    Never trade on a strategy you don't own."""

    class FakeTable:
        def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
            return {
                "Item": {
                    "pk": "STRATEGY#abc",
                    "strategy_id": "abc",
                    "org_id": "org-actual",
                    "author_user_id": "u1",
                    "name": "s",
                    "markdown": "x",
                    "symbols": ["AAPL"],
                    "capital": "100",
                    "status": "active",
                    "created_at": 1,
                    "updated_at": 1,
                },
            }

    with pytest.raises(RuntimeError, match="org_id mismatch"):
        load_single_bot_config(
            FakeTable(),
            env={"STRATEGY_ID": "abc", "ORG_ID": "org-attacker"},
        )


def test_load_single_bot_config_missing_strategy() -> None:
    """Strategy was deleted between ECS task start and app boot — exit
    cleanly rather than registering a phantom bot."""

    class FakeTable:
        def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
            return {}  # no Item key

    with pytest.raises(RuntimeError, match="not found"):
        load_single_bot_config(
            FakeTable(),
            env={"STRATEGY_ID": "abc", "ORG_ID": "org-1"},
        )


def test_load_single_bot_config_refuses_non_active_strategy() -> None:
    """The StrategySupervisor should never start a task for a non-active
    strategy, but if something races — a PAUSE lands after start — we
    exit rather than start trading."""

    class FakeTable:
        def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
            return {
                "Item": {
                    "pk": "STRATEGY#abc",
                    "strategy_id": "abc",
                    "org_id": "org-1",
                    "author_user_id": "u1",
                    "name": "s",
                    "markdown": "x",
                    "symbols": ["AAPL"],
                    "capital": "100",
                    "status": "paused",
                    "created_at": 1,
                    "updated_at": 1,
                },
            }

    with pytest.raises(RuntimeError, match="not active"):
        load_single_bot_config(
            FakeTable(),
            env={"STRATEGY_ID": "abc", "ORG_ID": "org-1"},
        )
