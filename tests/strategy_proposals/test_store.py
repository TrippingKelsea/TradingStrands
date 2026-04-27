"""Tests for StrategyProposalsStore.

Self-critique proposals are never auto-applied — the author (or
orgadmin) must explicitly apply or reject. The store enforces the
single-transition invariant: PENDING → APPLIED or PENDING → REJECTED,
never back to PENDING, never re-decide.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from trading_strands.strategy_proposals.store import (
    ProposalNotFoundError,
    ProposalStatus,
    StrategyProposalsStore,
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


def test_create_and_list() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique",
            proposer_agent_id="sc-s1",
            rationale="entry rules too broad",
            proposed_markdown="# refined rules",
        )
        assert p.status is ProposalStatus.PENDING
        assert p.strategy_id == "s1"
        entries = store.list_for_strategy("s1")
        assert len(entries) == 1
        assert entries[0].proposal_id == p.proposal_id


def test_get_returns_same_proposal() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        got = store.get("s1", p.proposal_id)
        assert got.proposal_id == p.proposal_id


def test_get_unknown_raises() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        with pytest.raises(ProposalNotFoundError):
            store.get("s1", "nonexistent")


def test_decide_applied_transitions_state() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        updated = store.decide(
            strategy_id="s1", proposal_id=p.proposal_id,
            status=ProposalStatus.APPLIED,
            decided_by="user-alice",
        )
        assert updated.status is ProposalStatus.APPLIED
        assert updated.decided_by == "user-alice"
        assert updated.decided_at > 0


def test_decide_rejected_transitions_state() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        updated = store.decide(
            strategy_id="s1", proposal_id=p.proposal_id,
            status=ProposalStatus.REJECTED,
            decided_by="user-alice",
        )
        assert updated.status is ProposalStatus.REJECTED


def test_cannot_transition_to_pending() -> None:
    """Proposals are single-decision — an APPLIED proposal never
    rolls back to PENDING. That would change the audit trail."""

    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        with pytest.raises(ValueError):
            store.decide(
                strategy_id="s1", proposal_id=p.proposal_id,
                status=ProposalStatus.PENDING,
                decided_by="user-alice",
            )


def test_cannot_re_decide_already_decided() -> None:
    """Applied proposals can't be rejected later (and vice versa).
    The conditional expression enforces the single-transition invariant
    at the DDB layer."""

    with mock_aws():
        store = StrategyProposalsStore(_table())
        p = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        store.decide(
            strategy_id="s1", proposal_id=p.proposal_id,
            status=ProposalStatus.APPLIED, decided_by="alice",
        )
        with pytest.raises(ClientError):
            store.decide(
                strategy_id="s1", proposal_id=p.proposal_id,
                status=ProposalStatus.REJECTED, decided_by="bob",
            )


def test_list_filters_by_status() -> None:
    with mock_aws():
        store = StrategyProposalsStore(_table())
        a = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="a", proposed_markdown="m",
        )
        b = store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="b", proposed_markdown="m",
        )
        store.decide(
            strategy_id="s1", proposal_id=a.proposal_id,
            status=ProposalStatus.APPLIED, decided_by="alice",
        )

        pending = store.list_for_strategy(
            "s1", status=ProposalStatus.PENDING,
        )
        assert [p.proposal_id for p in pending] == [b.proposal_id]

        applied = store.list_for_strategy(
            "s1", status=ProposalStatus.APPLIED,
        )
        assert [p.proposal_id for p in applied] == [a.proposal_id]


def test_list_for_strategy_is_isolated() -> None:
    """Proposals from one strategy don't leak into another's list."""

    with mock_aws():
        store = StrategyProposalsStore(_table())
        store.create(
            strategy_id="s1", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        store.create(
            strategy_id="s2", org_id="o1",
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="m",
        )
        assert len(store.list_for_strategy("s1")) == 1
        assert len(store.list_for_strategy("s2")) == 1
