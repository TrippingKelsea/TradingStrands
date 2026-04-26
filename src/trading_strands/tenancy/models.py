"""Tenancy domain models. Pydantic for validation at IO boundaries."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trading_strands.authz.model import Role


class OrgType(StrEnum):
    """Distinguishes the system org (Women with Super Powers) from
    customer orgs. Only members of a `system` org can be assigned
    sysadmin; customer orgs cannot hold sysadmin grants."""

    SYSTEM = "system"
    CUSTOMER = "customer"


class Org(BaseModel):
    """An organization. Customers get one; the platform has exactly one
    system org at any time (created at bootstrap).

    `settings` is intentionally loose — per-org config like session
    max-age, default capital, etc. Validated at read time by the caller.
    """

    model_config = ConfigDict(extra="forbid")

    org_id: str
    name: str
    org_type: OrgType = OrgType.CUSTOMER
    created_at: int
    updated_at: int
    settings: dict[str, object] = Field(default_factory=dict)


class User(BaseModel):
    """A first-class user record. `cognito_sub` is a pointer to Cognito;
    `user_id` is our identifier — preserved across Cognito pool rebuilds
    so foreign-key references (strategy authorship, memberships) survive
    auth infrastructure changes.

    `display_timezone` is UI-only. All timestamps at the API/DB boundary
    are UTC; this tells the frontend how to format them.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    email: str  # format not validated here; see TenancyStore for uniqueness
    cognito_sub: str | None = None
    display_name: str = ""
    display_timezone: str = "UTC"
    last_active_org_id: str | None = None
    created_at: int
    updated_at: int


class Membership(BaseModel):
    """A (user, org, role) triple. The row that unlocks capability X in
    org Y for user Z."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    org_id: str
    role: Role
    created_at: int
