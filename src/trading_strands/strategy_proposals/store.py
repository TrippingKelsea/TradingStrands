"""DDB store for self-critique's proposed strategy-prompt edits.

Schema:
    pk = STRATEGYPROPOSAL#{strategy_id}#{unix_ts}

Fields:
    strategy_id, proposer_agent, org_id, created_at, rationale,
    proposed_markdown, status (pending|applied|rejected),
    decided_by (user_id, empty until decided),
    decided_at (0 until decided)

Proposals are *never* auto-applied. Apply/reject are explicit user
actions; the store only tracks state transitions. See
docs/SPEC/agents.md §Self-Critique — authorship rules from
multi_tenancy.md apply, so the dashboard handler enforces the same
authz on apply as it does on strategy.update.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class ProposalNotFoundError(Exception):
    """Raised when a proposal lookup returns nothing."""


class StrategyProposal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    strategy_id: str
    org_id: str
    proposer_agent: str  # "self_critique" for v0
    proposer_agent_id: str  # bot-id or lambda run context
    created_at: int
    rationale: str
    proposed_markdown: str
    status: ProposalStatus = ProposalStatus.PENDING
    decided_by: str = ""
    decided_at: int = 0


def _pk(strategy_id: str, created_at: int, proposal_id: str) -> str:
    return f"STRATEGYPROPOSAL#{strategy_id}#{created_at}#{proposal_id}"


def _prefix_for_strategy(strategy_id: str) -> str:
    return f"STRATEGYPROPOSAL#{strategy_id}#"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class StrategyProposalsStore:
    """Writer + reader for STRATEGYPROPOSAL#* rows."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def create(
        self,
        *,
        strategy_id: str,
        org_id: str,
        proposer_agent: str,
        proposer_agent_id: str,
        rationale: str,
        proposed_markdown: str,
    ) -> StrategyProposal:
        ts = int(time.time())
        pid = _new_id()
        proposal = StrategyProposal(
            proposal_id=pid,
            strategy_id=strategy_id,
            org_id=org_id,
            proposer_agent=proposer_agent,
            proposer_agent_id=proposer_agent_id,
            created_at=ts,
            rationale=rationale,
            proposed_markdown=proposed_markdown,
            status=ProposalStatus.PENDING,
        )
        self._table.put_item(Item={
            "pk": _pk(strategy_id, ts, pid),
            **proposal.model_dump(mode="json"),
        })
        return proposal

    def _find_pk(self, strategy_id: str, proposal_id: str) -> str:
        """Locate the full pk for a (strategy_id, proposal_id). Needed
        because the pk includes the timestamp, and callers only know
        the proposal_id."""

        resp = self._table.scan(
            FilterExpression=(
                Attr("pk").begins_with(_prefix_for_strategy(strategy_id))
                & Attr("proposal_id").eq(proposal_id)
            ),
        )
        items = resp.get("Items", [])
        if not items:
            raise ProposalNotFoundError(proposal_id)
        return str(items[0]["pk"])

    def get(
        self, strategy_id: str, proposal_id: str,
    ) -> StrategyProposal:
        pk = self._find_pk(strategy_id, proposal_id)
        resp = self._table.get_item(Key={"pk": pk})
        item = resp.get("Item")
        if item is None:
            raise ProposalNotFoundError(proposal_id)
        return StrategyProposal.model_validate(
            {k: v for k, v in item.items() if k != "pk"},
        )

    def list_for_strategy(
        self, strategy_id: str, *,
        status: ProposalStatus | None = None,
        limit: int = 50,
    ) -> list[StrategyProposal]:
        """Newest-first. Optional `status` filter; default returns
        all statuses so the UI can show the full history."""

        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with(
                _prefix_for_strategy(strategy_id),
            ),
        )
        entries = [
            StrategyProposal.model_validate(
                {k: v for k, v in item.items() if k != "pk"},
            )
            for item in resp.get("Items", [])
        ]
        if status is not None:
            entries = [e for e in entries if e.status is status]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries[:limit]

    def decide(
        self,
        *,
        strategy_id: str,
        proposal_id: str,
        status: ProposalStatus,
        decided_by: str,
    ) -> StrategyProposal:
        """Transition a proposal from PENDING to APPLIED or REJECTED.

        No-op if the proposal is already in a terminal state — we
        never want to undo a prior decision or retroactively change
        the audit trail. Raises ProposalNotFoundError otherwise.
        """

        if status is ProposalStatus.PENDING:
            msg = "cannot transition a proposal back to pending"
            raise ValueError(msg)

        pk = self._find_pk(strategy_id, proposal_id)
        now = int(time.time())
        resp = self._table.update_item(
            Key={"pk": pk},
            UpdateExpression=(
                "SET #s = :new_status, "
                "decided_by = :who, decided_at = :now"
            ),
            # 'status' is a DDB reserved word.
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new_status": status.value,
                ":who": decided_by,
                ":now": now,
                ":pending": ProposalStatus.PENDING.value,
            },
            ConditionExpression=(
                "attribute_exists(pk) AND #s = :pending"
            ),
            ReturnValues="ALL_NEW",
        )
        attrs = resp.get("Attributes", {})
        return StrategyProposal.model_validate(
            {k: v for k, v in attrs.items() if k != "pk"},
        )
