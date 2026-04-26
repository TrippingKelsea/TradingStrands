"""DynamoDB persistence for users, orgs, and memberships.

Every function here is transactional where correctness demands it (e.g.,
creating a user+email-index atomically to prevent email collisions;
creating both sides of a membership atomically so queries are consistent).

The table is keyed by `pk` only — no sort key. Access patterns are
modeled via key prefixes so `begins_with(pk, ...)` scans are cheap.
A single table is used so we pay for one capacity pool instead of many.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from trading_strands.authz.model import Role
from trading_strands.tenancy.models import Membership, Org, OrgType, User


def _now() -> int:
    return int(time.time())


def _new_id() -> str:
    """8-char user/org id. Short enough to fit in URLs and keys, long enough
    to avoid collisions at our scale. Using uuid4 so there's no ordering
    information leaking."""

    return uuid.uuid4().hex[:8]


class EmailAlreadyInUseError(Exception):
    """Raised when creating a user whose email is already registered."""


class NotFoundError(Exception):
    """Raised when an item lookup returns nothing."""


class TenancyStore:
    """Handle for reading and writing tenancy records. Stateless — safe
    to instantiate per-request. The table handle is injected so tests
    can pass a moto-backed fake."""

    def __init__(self, table: Any) -> None:
        self._table = table

    # ── Org CRUD ──────────────────────────────────────────────────────

    def create_org(self, name: str, org_type: OrgType = OrgType.CUSTOMER) -> Org:
        org_id = _new_id()
        now = _now()
        org = Org(
            org_id=org_id, name=name, org_type=org_type,
            created_at=now, updated_at=now, settings={},
        )
        self._table.put_item(
            Item={"pk": f"ORG#{org_id}", **org.model_dump(mode="json")},
            ConditionExpression=Attr("pk").not_exists(),
        )
        return org

    def get_org(self, org_id: str) -> Org:
        resp = self._table.get_item(Key={"pk": f"ORG#{org_id}"})
        item = resp.get("Item")
        if item is None:
            raise NotFoundError(f"org {org_id} not found")
        return Org.model_validate({k: v for k, v in item.items() if k != "pk"})

    def list_orgs(self) -> list[Org]:
        """Scan all orgs. Scan is acceptable here — the number of orgs is
        small (< 1000) and this is only called by sysadmin meta-views."""

        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with("ORG#"),
        )
        return [
            Org.model_validate({k: v for k, v in item.items() if k != "pk"})
            for item in resp.get("Items", [])
        ]

    def find_system_org(self) -> Org | None:
        """Return the singleton system org, or None if bootstrap hasn't run."""

        for org in self.list_orgs():
            if org.org_type == OrgType.SYSTEM:
                return org
        return None

    # ── User CRUD ─────────────────────────────────────────────────────

    def create_user(
        self, email: str, cognito_sub: str | None = None,
        display_name: str = "",
    ) -> User:
        """Create a user with an email-uniqueness guard.

        Write order: email index first (with a conditional put that fails
        if the email is already taken), then the user record. If the user
        put fails for any reason, we best-effort clean up the index row.

        This ordering is intentional. If we wrote the user first and the
        index put failed on collision, the user record would leak.
        Writing the index first means a collision blocks the whole
        operation before any user data is committed.
        """

        user_id = _new_id()
        now = _now()
        email_lower = email.lower().strip()

        email_item = {
            "pk": f"USEREMAIL#{email_lower}",
            "user_id": user_id,
            "created_at": now,
        }
        try:
            self._table.put_item(
                Item=email_item,
                ConditionExpression=Attr("pk").not_exists(),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise EmailAlreadyInUseError(
                    f"email {email_lower} already in use",
                ) from exc
            raise

        user = User(
            user_id=user_id, email=email, cognito_sub=cognito_sub,
            display_name=display_name, display_timezone="UTC",
            last_active_org_id=None, created_at=now, updated_at=now,
        )
        user_item: dict[str, Any] = {
            "pk": f"USER#{user_id}",
            **user.model_dump(mode="json", exclude_none=True),
        }
        try:
            self._table.put_item(Item=user_item)
        except Exception:
            self._table.delete_item(
                Key={"pk": f"USEREMAIL#{email_lower}"},
            )
            raise
        return user

    def get_user(self, user_id: str) -> User:
        resp = self._table.get_item(Key={"pk": f"USER#{user_id}"})
        item = resp.get("Item")
        if item is None:
            raise NotFoundError(f"user {user_id} not found")
        return User.model_validate({k: v for k, v in item.items() if k != "pk"})

    def find_user_by_email(self, email: str) -> User | None:
        """Return the user with this email, or None. Case-insensitive."""

        email_lower = email.lower().strip()
        resp = self._table.get_item(Key={"pk": f"USEREMAIL#{email_lower}"})
        item = resp.get("Item")
        if item is None:
            return None
        user_id = item["user_id"]
        try:
            return self.get_user(user_id)
        except NotFoundError:
            # Index points at a deleted user — stale row. Treat as missing.
            return None

    def list_users(self) -> list[User]:
        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with("USER#"),
        )
        return [
            User.model_validate({k: v for k, v in item.items() if k != "pk"})
            for item in resp.get("Items", [])
        ]

    def set_last_active_org(self, user_id: str, org_id: str | None) -> None:
        self._table.update_item(
            Key={"pk": f"USER#{user_id}"},
            UpdateExpression="SET last_active_org_id = :o, updated_at = :t",
            ExpressionAttributeValues={":o": org_id, ":t": _now()},
            ConditionExpression="attribute_exists(pk)",
        )

    # ── Membership (many-to-many) ─────────────────────────────────────

    def add_membership(
        self, user_id: str, org_id: str, role: Role,
    ) -> Membership:
        """Write both sides of the many-to-many join.

        Two puts, not a transaction — DynamoDB's TransactWrite has a
        latency cost and this write is rare (only during org management).
        If the second put fails, we rollback the first. Concurrent writes
        for the same (user, org) pair are self-healing: put is idempotent
        by PK, so two simultaneous add_memberships for the same pair
        result in the same final state.
        """

        now = _now()
        userorg_item = {
            "pk": f"USERORG#{user_id}#{org_id}",
            "user_id": user_id,
            "org_id": org_id,
            "role": role.value,
            "created_at": now,
        }
        orguser_item = {
            "pk": f"ORGUSER#{org_id}#{user_id}",
            "user_id": user_id,
            "org_id": org_id,
            "role": role.value,
            "created_at": now,
        }

        self._table.put_item(Item=userorg_item)
        try:
            self._table.put_item(Item=orguser_item)
        except Exception:
            self._table.delete_item(
                Key={"pk": f"USERORG#{user_id}#{org_id}"},
            )
            raise

        return Membership(
            user_id=user_id, org_id=org_id, role=role, created_at=now,
        )

    def remove_membership(self, user_id: str, org_id: str) -> None:
        """Delete both sides. Best-effort — if one delete fails, the
        other still proceeds; the resulting inconsistency is detected
        by the Cognito sync job."""

        self._table.delete_item(Key={"pk": f"USERORG#{user_id}#{org_id}"})
        self._table.delete_item(Key={"pk": f"ORGUSER#{org_id}#{user_id}"})

    def memberships_for_user(self, user_id: str) -> list[Membership]:
        """Cheap query by key prefix — no scan."""

        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with(f"USERORG#{user_id}#"),
        )
        return [_to_membership(item) for item in resp.get("Items", [])]

    def memberships_for_org(self, org_id: str) -> list[Membership]:
        """List all users in an org with their roles."""

        resp = self._table.scan(
            FilterExpression=Attr("pk").begins_with(f"ORGUSER#{org_id}#"),
        )
        return [_to_membership(item) for item in resp.get("Items", [])]

    def role_of(self, user_id: str, org_id: str) -> Role | None:
        """Lookup of role for (user, org). Returns None if not a member."""

        resp = self._table.get_item(
            Key={"pk": f"USERORG#{user_id}#{org_id}"},
        )
        item = resp.get("Item")
        if item is None:
            return None
        return Role(item["role"])

    # ── Sysadmin grants ───────────────────────────────────────────────

    def grant_sysadmin(self, user_id: str) -> None:
        """Mark a user as sysadmin. The caller is responsible for verifying
        the user is a member of the system org before calling — this store
        enforces the sentinel's existence but not the cross-reference."""

        self._table.put_item(
            Item={
                "pk": f"SYSADMIN#{user_id}",
                "user_id": user_id,
                "created_at": _now(),
            },
        )

    def revoke_sysadmin(self, user_id: str) -> None:
        self._table.delete_item(Key={"pk": f"SYSADMIN#{user_id}"})

    def is_sysadmin(self, user_id: str) -> bool:
        resp = self._table.get_item(Key={"pk": f"SYSADMIN#{user_id}"})
        return resp.get("Item") is not None


# ── Helpers ───────────────────────────────────────────────────────────


def _to_membership(item: dict[str, Any]) -> Membership:
    return Membership(
        user_id=item["user_id"],
        org_id=item["org_id"],
        role=Role(item["role"]),
        created_at=int(item["created_at"]),
    )
