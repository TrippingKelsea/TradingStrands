"""Tests for SkillsStore + compose_system_prompt.

SPEC/tools.md §8. Per-org skill storage, name-based lookup, 32 KB
body cap, no versioning. Composition assembles the labelled sections
shape in Strategy.skills order.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.skills_store.store import (
    Skill,
    SkillNotFoundError,
    SkillsStore,
    SkillTooLargeError,
    compose_system_prompt,
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


# ── Store CRUD ─────────────────────────────────────────────────────


def test_put_and_get_round_trip() -> None:
    with mock_aws():
        store = SkillsStore(_table())
        store.put(
            org_id="o1", skill_name="morning_prep",
            markdown="## Morning Prep\n\nCheck overnight.",
            author_user_id="u1",
        )
        loaded = store.get("o1", "morning_prep")
        assert loaded.org_id == "o1"
        assert loaded.skill_name == "morning_prep"
        assert "overnight" in loaded.markdown
        assert loaded.author_user_id == "u1"
        assert loaded.created_at > 0
        assert loaded.updated_at > 0


def test_get_missing_raises() -> None:
    with mock_aws():
        store = SkillsStore(_table())
        with pytest.raises(SkillNotFoundError):
            store.get("o1", "nope")


def test_put_updates_existing_skill() -> None:
    """Orgadmin edits a skill → updated_at advances, created_at stays."""

    import time

    with mock_aws():
        store = SkillsStore(_table())
        store.put(
            org_id="o1", skill_name="m",
            markdown="v1", author_user_id="u1",
        )
        first = store.get("o1", "m")
        time.sleep(1.05)
        store.put(
            org_id="o1", skill_name="m",
            markdown="v2", author_user_id="u1",
        )
        second = store.get("o1", "m")
        assert second.markdown == "v2"
        assert second.created_at == first.created_at
        assert second.updated_at > first.updated_at


def test_delete_removes_skill() -> None:
    with mock_aws():
        store = SkillsStore(_table())
        store.put(
            org_id="o1", skill_name="m",
            markdown="x", author_user_id="u1",
        )
        store.delete("o1", "m")
        with pytest.raises(SkillNotFoundError):
            store.get("o1", "m")


def test_delete_missing_is_noop() -> None:
    """Operators will sometimes click delete twice. Second delete
    must not raise — idempotent semantics."""

    with mock_aws():
        store = SkillsStore(_table())
        store.delete("o1", "never_existed")   # no raise


def test_list_for_org_returns_only_that_orgs_skills() -> None:
    with mock_aws():
        store = SkillsStore(_table())
        store.put(
            org_id="o1", skill_name="a",
            markdown="m", author_user_id="u1",
        )
        store.put(
            org_id="o1", skill_name="b",
            markdown="m", author_user_id="u1",
        )
        store.put(
            org_id="o2", skill_name="c",
            markdown="m", author_user_id="u2",
        )
        skills = store.list_for_org("o1")
        assert {s.skill_name for s in skills} == {"a", "b"}


def test_list_for_org_empty_returns_empty_list() -> None:
    with mock_aws():
        store = SkillsStore(_table())
        assert store.list_for_org("o1") == []


def test_put_rejects_oversized_skill() -> None:
    """§8.2 caps skills at 32 KB. Oversized → SkillTooLargeError,
    fail-closed rather than truncating silently."""

    with mock_aws():
        store = SkillsStore(_table())
        too_big = "x" * (32 * 1024 + 1)
        with pytest.raises(SkillTooLargeError):
            store.put(
                org_id="o1", skill_name="big",
                markdown=too_big, author_user_id="u1",
            )


def test_skill_name_uniqueness_scoped_to_org() -> None:
    """Two orgs can both have a 'morning_prep' — names only need to
    be unique within an org."""

    with mock_aws():
        store = SkillsStore(_table())
        store.put(
            org_id="o1", skill_name="morning_prep",
            markdown="o1 version", author_user_id="u1",
        )
        store.put(
            org_id="o2", skill_name="morning_prep",
            markdown="o2 version", author_user_id="u2",
        )
        assert store.get("o1", "morning_prep").markdown == "o1 version"
        assert store.get("o2", "morning_prep").markdown == "o2 version"


# ── compose_system_prompt ──────────────────────────────────────────


def test_compose_with_no_skills_returns_base_plus_strategy() -> None:
    out = compose_system_prompt(
        base_prompt="You are disciplined.",
        skills=[],
        strategy_name="MyStrat",
        strategy_markdown="buy on dips",
    )
    assert "You are disciplined." in out
    assert "# Strategy: MyStrat" in out
    assert "buy on dips" in out
    # No skill sections.
    assert "# Skill:" not in out


def test_compose_preserves_skill_order() -> None:
    """§8.4: skill order follows Strategy.skills order so authors can
    build progressive context (general first, specific last)."""

    skill_a = Skill(
        org_id="o1", skill_name="alpha",
        markdown="alpha body", author_user_id="u1",
        created_at=1, updated_at=1,
    )
    skill_b = Skill(
        org_id="o1", skill_name="beta",
        markdown="beta body", author_user_id="u1",
        created_at=1, updated_at=1,
    )
    out = compose_system_prompt(
        base_prompt="base",
        skills=[skill_a, skill_b],
        strategy_name="S",
        strategy_markdown="strat",
    )
    # Alpha appears before beta.
    alpha_idx = out.index("# Skill: alpha")
    beta_idx = out.index("# Skill: beta")
    strat_idx = out.index("# Strategy:")
    assert alpha_idx < beta_idx < strat_idx


def test_compose_renders_skill_body_verbatim() -> None:
    """Skill markdown is embedded as-is; no parsing or transformation.
    Authors see what the LLM sees."""

    skill = Skill(
        org_id="o1", skill_name="greeks",
        markdown="## Delta\n\n- ATM calls ≈ 0.5",
        author_user_id="u1", created_at=1, updated_at=1,
    )
    out = compose_system_prompt(
        base_prompt="b",
        skills=[skill],
        strategy_name="S",
        strategy_markdown="x",
    )
    assert "## Delta" in out
    assert "ATM calls ≈ 0.5" in out


def test_compose_strategy_markdown_last() -> None:
    """Strategy markdown is always last — the most specific context.
    Skills frame general knowledge; the strategy is what the LLM is
    supposed to execute."""

    skill = Skill(
        org_id="o1", skill_name="m",
        markdown="skill body",
        author_user_id="u1", created_at=1, updated_at=1,
    )
    out = compose_system_prompt(
        base_prompt="b",
        skills=[skill],
        strategy_name="S",
        strategy_markdown="strategy body",
    )
    assert out.index("skill body") < out.index("strategy body")
