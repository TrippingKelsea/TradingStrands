"""Load an authz Principal from a session cookie.

This is the bridge between the HTTP layer (which sees a signed session
cookie) and the authorization layer (which needs a full Principal with
memberships). Every authorization decision flows through here.

Memberships are loaded per-request from DynamoDB. We accept the extra
round-trip in exchange for immediate revocation: if an admin removes a
user from an org, the next request reflects that change — no cached
state, no stale permissions.
"""

from __future__ import annotations

from typing import Any

from trading_strands.authz.model import Principal
from trading_strands.tenancy.store import NotFoundError, TenancyStore


class SessionInvalidError(Exception):
    """Raised when the session doesn't resolve to a valid user."""


def principal_from_session(
    session: dict[str, Any], table: Any,
) -> Principal:
    """Build a Principal from a validated session dict and a DDB handle.

    The session must carry `user_id`. Other attributes (email, active_org)
    are informational — the authoritative source is the DynamoDB user
    record. If the user_id doesn't resolve, this raises SessionInvalidError
    and the caller should treat the session as stale.
    """

    user_id = session.get("user_id")
    if not user_id:
        msg = "session missing user_id"
        raise SessionInvalidError(msg)

    store = TenancyStore(table)
    try:
        user = store.get_user(user_id)
    except NotFoundError as exc:
        msg = f"user {user_id} no longer exists"
        raise SessionInvalidError(msg) from exc

    memberships_list = store.memberships_for_user(user_id)
    memberships = {m.org_id: m.role for m in memberships_list}
    sysadmin = store.is_sysadmin(user_id)

    return Principal(
        user_id=user.user_id,
        email=user.email,
        memberships=memberships,
        sysadmin=sysadmin,
    )


def active_org_id(session: dict[str, Any], principal: Principal) -> str | None:
    """Determine the org the session is currently scoped to.

    Preference order:
      1. `active_org_id` in the session cookie — explicit user choice.
      2. The user's `last_active_org_id` (set at login / last org switch).
      3. If the user is in exactly one org, that org.
      4. None — caller must prompt for org selection.

    The result is validated: if the session claims an org the user is no
    longer a member of, we fall through as if there was no claim. This
    handles the "removed from org mid-session" case gracefully without
    leaking cross-org data.
    """

    claimed = session.get("active_org_id")
    if claimed and claimed in principal.memberships:
        return str(claimed)

    if len(principal.memberships) == 1:
        return next(iter(principal.memberships))

    return None
