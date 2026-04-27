"""Strategy DynamoDB store. Org-scoped reads, author-attributed writes."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from boto3.dynamodb.conditions import Attr
from pydantic import BaseModel, ConfigDict, Field

from trading_strands.authz.model import Action, Resource, ResourceType
from trading_strands.authz.policy import can
from trading_strands.tools.base import StrategyToolConfig


class StrategyStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class Strategy(BaseModel):
    """A trading strategy owned by an org and authored by a user.

    extra='ignore' so legacy pre-refactor strategies can be read by
    bootstrap's delete_legacy_strategies path before being pruned.
    New optional fields (tools, skills) default to empty so legacy
    rows without them load without a migration pass.
    """

    model_config = ConfigDict(extra="ignore")

    strategy_id: str
    org_id: str
    author_user_id: str
    name: str
    markdown: str
    symbols: list[str] = Field(default_factory=list)
    capital: str = "1000"
    status: StrategyStatus = StrategyStatus.ACTIVE
    created_at: int
    updated_at: int
    # See docs/SPEC/tools.md §4. Per-strategy tool opt-in + quota.
    # Default empty → strategy has no tools beyond the base LLM
    # reasoning. Legacy rows persist as-is until updated.
    tools: dict[str, StrategyToolConfig] = Field(default_factory=dict)
    # See docs/SPEC/tools.md §8. Skill names the strategy pulls into
    # its system prompt. Missing-skill lookup warns + skips, doesn't
    # block.
    skills: list[str] = Field(default_factory=list)
    # Allowlisted model id the bot should use. Empty → use the
    # platform default (trading_strands.models.registry
    # DEFAULT_MODEL_ID). Validation runs at create/update time;
    # legacy rows with missing / unknown ids resolve to default at
    # bot-start with a warning.
    model_id: str = ""


class StrategyACL(BaseModel):
    """Co-author grant. Indicates user_id has edit rights on strategy_id."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    user_id: str
    granted_by: str
    created_at: int


class StrategyNotFoundError(Exception):
    """Raised when a strategy lookup returns nothing."""


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _scan_all(table: Any, filter_expression: Any) -> list[dict[str, Any]]:
    """Exhaustively scan a table with a filter, following LastEvaluatedKey.

    Single-PK tables grow past 1 MB fast when they mix many prefix
    families (STRATEGY#, ORG#, MARKETDATA#, CALENDAR#, …). DDB scan
    returns at most ~1 MB of pre-filter items per call, so a one-shot
    scan can drop matches silently when the matches happen to live
    outside the first page of read.

    Observed symptom: list_all returned 1 of 2 ACTIVE strategies,
    which made reconcile_all skip provisioning a per-bot Fargate
    service for the second strategy. Pagination closes that gap.
    """

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"FilterExpression": filter_expression}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


class StrategyStore:
    """Persistence for strategies. Stateless; inject a table handle."""

    def __init__(self, table: Any) -> None:
        self._table = table

    # ── CRUD ──────────────────────────────────────────────────────────

    def create(
        self,
        org_id: str,
        author_user_id: str,
        name: str,
        markdown: str,
        symbols: list[str] | None = None,
        capital: str = "1000",
        tools: dict[str, StrategyToolConfig] | None = None,
        skills: list[str] | None = None,
        model_id: str = "",
    ) -> Strategy:
        # Validate model choice at save time — typos fail here, not
        # silently at bot-start. Empty string is legal (= use default).
        from trading_strands.models.registry import validate_model_id
        validate_model_id(model_id)
        strat = Strategy(
            strategy_id=_new_id(),
            org_id=org_id,
            author_user_id=author_user_id,
            name=name,
            markdown=markdown,
            symbols=symbols or [],
            capital=capital,
            status=StrategyStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
            tools=tools or {},
            skills=skills or [],
            model_id=model_id,
        )
        self._table.put_item(
            Item={
                "pk": f"STRATEGY#{strat.strategy_id}",
                **strat.model_dump(mode="json"),
            },
            ConditionExpression=Attr("pk").not_exists(),
        )
        return strat

    def get(self, strategy_id: str) -> Strategy:
        """Raw read — returns the strategy regardless of org. Callers
        MUST check authorization (typically via `resource_for()` + authz.can).
        """

        resp = self._table.get_item(Key={"pk": f"STRATEGY#{strategy_id}"})
        item = resp.get("Item")
        if item is None:
            raise StrategyNotFoundError(strategy_id)
        return Strategy.model_validate({k: v for k, v in item.items() if k != "pk"})

    def list_for_org(self, org_id: str) -> list[Strategy]:
        """Return all strategies owned by the given org. The filter on
        org_id is applied server-side; cross-org data physically cannot
        be returned from this method."""

        items = _scan_all(
            self._table,
            Attr("pk").begins_with("STRATEGY#") & Attr("org_id").eq(org_id),
        )
        return [
            Strategy.model_validate({k: v for k, v in item.items() if k != "pk"})
            for item in items
        ]

    def list_all(self) -> list[Strategy]:
        """Return every strategy in the system. Only callers with sysadmin
        + the SYSADMIN_CAN_READ_ORG_DATA deploy flag should use this; the
        store does not enforce that — the caller does."""

        items = _scan_all(
            self._table, Attr("pk").begins_with("STRATEGY#"),
        )
        return [
            Strategy.model_validate({k: v for k, v in item.items() if k != "pk"})
            for item in items
        ]

    def update(self, strategy_id: str, fields: dict[str, Any]) -> Strategy:
        """Apply a partial update. Unknown fields are ignored; org_id and
        author_user_id are never updatable — those are set once at create."""

        protected = {"pk", "strategy_id", "org_id", "author_user_id", "created_at"}
        fields = {k: v for k, v in fields.items() if k not in protected}
        if not fields:
            # No-op update — still bump updated_at.
            fields = {}

        # Allowlist validation on model changes — same as create.
        if "model_id" in fields:
            from trading_strands.models.registry import validate_model_id
            validate_model_id(str(fields["model_id"]))

        update_parts: list[str] = ["updated_at = :t"]
        names: dict[str, str] = {}
        values: dict[str, Any] = {":t": _now()}
        for i, (k, v) in enumerate(fields.items()):
            placeholder = f":v{i}"
            name_ref = f"#n{i}"
            names[name_ref] = k
            values[placeholder] = v
            update_parts.append(f"{name_ref} = {placeholder}")

        kwargs: dict[str, Any] = {
            "Key": {"pk": f"STRATEGY#{strategy_id}"},
            "UpdateExpression": "SET " + ", ".join(update_parts),
            "ExpressionAttributeValues": values,
            "ConditionExpression": "attribute_exists(pk)",
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        resp = self._table.update_item(**kwargs)
        attrs = resp.get("Attributes", {})
        return Strategy.model_validate({k: v for k, v in attrs.items() if k != "pk"})

    def delete(self, strategy_id: str) -> None:
        self._table.delete_item(Key={"pk": f"STRATEGY#{strategy_id}"})
        # Best-effort cleanup of ACLs for this strategy.
        items = _scan_all(
            self._table,
            Attr("pk").begins_with(f"STRATEGYACL#{strategy_id}#"),
        )
        for item in items:
            self._table.delete_item(Key={"pk": item["pk"]})

    # ── ACL ───────────────────────────────────────────────────────────

    def add_acl(
        self, strategy_id: str, user_id: str, granted_by: str,
    ) -> StrategyACL:
        acl = StrategyACL(
            strategy_id=strategy_id, user_id=user_id,
            granted_by=granted_by, created_at=_now(),
        )
        self._table.put_item(
            Item={
                "pk": f"STRATEGYACL#{strategy_id}#{user_id}",
                **acl.model_dump(mode="json"),
            },
        )
        return acl

    def remove_acl(self, strategy_id: str, user_id: str) -> None:
        self._table.delete_item(
            Key={"pk": f"STRATEGYACL#{strategy_id}#{user_id}"},
        )

    def acl_users(self, strategy_id: str) -> frozenset[str]:
        """Return the set of co-author user_ids for this strategy."""

        items = _scan_all(
            self._table,
            Attr("pk").begins_with(f"STRATEGYACL#{strategy_id}#"),
        )
        return frozenset(item["user_id"] for item in items)


# ── Authz bridge ──────────────────────────────────────────────────────


def resource_for(strategy: Strategy, acl: frozenset[str] = frozenset()) -> Resource:
    """Build the authz Resource for a loaded strategy.

    `acl` should be the result of `StrategyStore.acl_users(strategy_id)`
    so delegated-author grants are considered by the policy.
    """

    return Resource(
        type=ResourceType.STRATEGY,
        org_id=strategy.org_id,
        author_user_id=strategy.author_user_id,
        delegated_user_ids=acl,
    )


def resource_for_new(org_id: str, author_user_id: str) -> Resource:
    """Resource for the CREATE action — no strategy exists yet."""

    return Resource(
        type=ResourceType.STRATEGY,
        org_id=org_id,
        author_user_id=author_user_id,
    )


def can_perform(
    principal: Any,  # Principal, kept loose to avoid circular import noise
    action: Action,
    strategy: Strategy | None = None,
    org_id: str | None = None,
    acl: frozenset[str] = frozenset(),
) -> bool:
    """Convenience wrapper: build the Resource and delegate to authz."""

    if strategy is None:
        if org_id is None:
            msg = "need either strategy or org_id"
            raise ValueError(msg)
        resource = resource_for_new(org_id, principal.user_id)
    else:
        resource = resource_for(strategy, acl)
    return can(principal, action, resource).allowed
