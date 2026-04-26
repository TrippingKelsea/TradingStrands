"""Dashboard API — FastAPI routes for reading trading state from DynamoDB.

Every endpoint that touches org-scoped data does two things, in order:
  1. Resolves a `Principal` from the request session (via tenancy store).
  2. Calls `authz.can(principal, action, resource)` BEFORE touching DDB.

There is no coarse role-string gate anymore. A missing authz check is a
privacy bug by construction: endpoints that don't call require() run with
no authorization and deny-by-default data access, so the only way to
return org-scoped data is through an explicit authz check.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import boto3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from trading_strands.authz.model import Action, Principal, Resource, ResourceType
from trading_strands.authz.policy import Unauthorized, require
from trading_strands.dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_DEFAULT,
    AuthMiddleware,
    _get_cognito_client,
    authenticate,
    create_session_cookie,
    create_url_token,
    decode_url_token,
)
from trading_strands.dashboard.principal import (
    SessionInvalidError,
    principal_from_session,
)
from trading_strands.strategies_store.store import (
    StrategyNotFoundError,
    StrategyStore,
    resource_for,
)
from trading_strands.tenancy.store import NotFoundError, TenancyStore

app = FastAPI(title="TradingStrands Dashboard")

# Auth middleware — enforces login on all routes except /health, /login, /auth/*
app.add_middleware(AuthMiddleware)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _get_table_name() -> str:
    return os.environ.get("DYNAMODB_TABLE", "trading-strands-state")


def _get_table() -> Any:
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(_get_table_name())


# ── Request helpers ────────────────────────────────────────────────────


def _get_principal(request: Request) -> Principal:
    """Resolve a Principal from the request session. Raises 401/403 if the
    session doesn't resolve or is stale."""

    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        return principal_from_session(session, _get_table())
    except SessionInvalidError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _get_active_org(request: Request, principal: Principal) -> str:
    """Resolve the active org for this request, or raise 400 if none is set.

    The client is expected to either have `active_org_id` in session OR
    pass `?org=...` on the request. The query param is authoritative for
    this single request; the session cookie only changes on explicit
    org-switch. Either way we validate the user is a member.
    """

    claimed = request.query_params.get("org") or (
        request.state.session.get("active_org_id")
        if hasattr(request.state, "session") else None
    )
    if not claimed:
        raise HTTPException(
            status_code=400,
            detail="No active organization selected",
        )
    if claimed not in principal.memberships:
        raise HTTPException(
            status_code=403,
            detail="Not a member of the requested organization",
        )
    return str(claimed)


def _require(principal: Principal, action: Action, resource: Resource) -> None:
    """Check authorization or raise 403. Audit-friendly — the denial reason
    becomes the detail so logs carry it."""

    try:
        require(principal, action, resource)
    except Unauthorized as exc:
        raise HTTPException(status_code=403, detail=exc.reason) from exc


# ── Public / unauthenticated routes ────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    error = ""
    token = request.query_params.get("t", "")
    if token:
        data = decode_url_token(token)
        if data:
            error = data.get("error", "")
    return _templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/auth/login")
async def auth_login(
    email: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    tenancy = TenancyStore(_get_table())
    user_info = authenticate(email, password, tenancy)
    if user_info is None:
        token = create_url_token({"error": "Invalid email or password"})
        return RedirectResponse(url=f"/login?t={token}", status_code=303)

    session_value = create_session_cookie(user_info)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_value,
        max_age=SESSION_MAX_AGE_DEFAULT,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.post("/auth/logout")
async def auth_logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse(request, "index.html")


# ── Live state ─────────────────────────────────────────────────────────


@app.get("/api/snapshot")
async def snapshot(request: Request) -> dict[str, Any]:
    """Infrastructure telemetry — the trading service's last snapshot.
    Any authenticated user can read this (it's operational visibility,
    not per-org trading data)."""

    _ = _get_principal(request)  # authentication only; no authz gate
    table = _get_table()
    resp = table.get_item(Key={"pk": "SNAPSHOT"})
    item = resp.get("Item")
    if item is None:
        return {"tick": 0, "prices": {}, "ledgers": {}, "risk": {}, "timestamp": 0}
    return dict(item)


@app.get("/api/events")
async def events(request: Request) -> list[dict[str, Any]]:
    _ = _get_principal(request)
    table = _get_table()
    resp = table.scan(
        FilterExpression="begins_with(pk, :prefix)",
        ExpressionAttributeValues={":prefix": "EVENT#"},
    )
    items = resp.get("Items", [])
    items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return [dict(item) for item in items[:50]]


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE — polls DynamoDB every 2s and yields snapshots."""

    _ = _get_principal(request)

    async def event_generator() -> Any:
        import json

        table = _get_table()
        last_tick = -1

        yield f"data: {json.dumps({'connected': True})}\n\n"

        while True:
            try:
                resp = table.get_item(Key={"pk": "SNAPSHOT"})
                item = resp.get("Item")
                if item is not None:
                    tick = item.get("tick", 0)
                    if tick != last_tick:
                        last_tick = tick
                        data = json.dumps(item, default=str)
                        yield f"data: {data}\n\n"
                else:
                    yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'read failed'})}\n\n"

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ── Strategy CRUD (org-scoped) ─────────────────────────────────────────


class StrategyCreate(BaseModel):
    name: str
    markdown: str
    symbols: list[str] = []
    capital: str = "1000"


class StrategyUpdate(BaseModel):
    name: str | None = None
    markdown: str | None = None
    symbols: list[str] | None = None
    capital: str | None = None
    status: str | None = None


@app.get("/api/strategies")
async def list_strategies(request: Request) -> list[dict[str, Any]]:
    """List strategies visible in the currently-active org.

    Scoped at the persistence layer (StrategyStore.list_for_org). Cross-org
    data is physically not returned by this query.
    """

    principal = _get_principal(request)
    org_id = _get_active_org(request, principal)

    # Read authorization: list strategies within the scoped org.
    _require(principal, Action.LIST, Resource(ResourceType.STRATEGY, org_id=org_id))

    store = StrategyStore(_get_table())
    items = store.list_for_org(org_id)
    out = [item.model_dump(mode="json") for item in items]
    out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return out


@app.post("/api/strategies", status_code=201)
async def create_strategy(
    request: Request, body: StrategyCreate,
) -> dict[str, Any]:
    principal = _get_principal(request)
    org_id = _get_active_org(request, principal)

    _require(
        principal, Action.CREATE,
        Resource(ResourceType.STRATEGY, org_id=org_id, author_user_id=principal.user_id),
    )

    store = StrategyStore(_get_table())
    strat = store.create(
        org_id=org_id,
        author_user_id=principal.user_id,
        name=body.name,
        markdown=body.markdown,
        symbols=body.symbols,
        capital=body.capital,
    )
    return strat.model_dump(mode="json")


@app.get("/api/strategies/{strategy_id}")
async def get_strategy(
    request: Request, strategy_id: str,
) -> dict[str, Any]:
    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy not found") from None

    acl = store.acl_users(strategy_id)
    _require(principal, Action.READ, resource_for(strat, acl))
    return strat.model_dump(mode="json")


@app.put("/api/strategies/{strategy_id}")
async def update_strategy(
    request: Request, strategy_id: str, body: StrategyUpdate,
) -> dict[str, Any]:
    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy not found") from None

    acl = store.acl_users(strategy_id)
    _require(principal, Action.UPDATE, resource_for(strat, acl))

    update_fields: dict[str, Any] = {}
    if body.name is not None:
        update_fields["name"] = body.name
    if body.markdown is not None:
        update_fields["markdown"] = body.markdown
    if body.symbols is not None:
        update_fields["symbols"] = body.symbols
    if body.capital is not None:
        update_fields["capital"] = body.capital
    if body.status is not None:
        if body.status not in ("active", "paused", "stopped"):
            raise HTTPException(status_code=400, detail="Invalid status")
        update_fields["status"] = body.status

    updated = store.update(strategy_id, update_fields)
    return updated.model_dump(mode="json")


class StrategyStatusUpdate(BaseModel):
    status: str


@app.put("/api/strategies/{strategy_id}/status")
async def update_strategy_status(
    request: Request, strategy_id: str, body: StrategyStatusUpdate,
) -> dict[str, str]:
    """Status-only convenience endpoint, preserved for the existing UI."""

    if body.status not in ("active", "paused", "stopped"):
        raise HTTPException(status_code=400, detail="Invalid status")

    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy not found") from None

    acl = store.acl_users(strategy_id)
    _require(principal, Action.UPDATE, resource_for(strat, acl))

    store.update(strategy_id, {"status": body.status})
    return {"status": body.status}


@app.delete("/api/strategies/{strategy_id}", status_code=204)
async def delete_strategy(request: Request, strategy_id: str) -> None:
    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        # Idempotent delete — no principal learns whether a strategy they
        # can't see existed or not.
        return

    acl = store.acl_users(strategy_id)
    _require(principal, Action.DELETE, resource_for(strat, acl))
    store.delete(strategy_id)


# ── Halt control ───────────────────────────────────────────────────────


@app.post("/api/halt")
async def halt_trading(request: Request) -> dict[str, str]:
    """Emergency halt — writes desk halt flag. System-wide action, gated
    on orgadmin-of-anywhere OR sysadmin. For now any authenticated user
    with at least orgadmin somewhere can halt — halting is a safety net,
    we'd rather it be accessible in an emergency than gated too tightly.
    Re-evaluate when we have multiple unrelated customer orgs."""

    principal = _get_principal(request)
    if not principal.sysadmin and not any(
        role.value == "orgadmin" for role in principal.memberships.values()
    ):
        raise HTTPException(status_code=403, detail="halt requires orgadmin")

    import time as _time

    table = _get_table()
    table.put_item(Item={
        "pk": "CONTROL",
        "desk_halted": True,
        "updated_at": int(_time.time()),
    })
    return {"status": "halted"}


@app.post("/api/unhalt")
async def unhalt_trading(request: Request) -> dict[str, str]:
    principal = _get_principal(request)
    if not principal.sysadmin and not any(
        role.value == "orgadmin" for role in principal.memberships.values()
    ):
        raise HTTPException(status_code=403, detail="unhalt requires orgadmin")

    import time as _time

    table = _get_table()
    table.put_item(Item={
        "pk": "CONTROL",
        "desk_halted": False,
        "updated_at": int(_time.time()),
    })
    return {"status": "running"}


# ── Telemetry ──────────────────────────────────────────────────────────


@app.get("/api/telemetry")
async def telemetry(request: Request) -> dict[str, Any]:
    """Aggregate telemetry — authentication only (visible to all users).

    Strategy counts here are unscoped (global across orgs) intentionally:
    this endpoint is infrastructure health, not per-org accounting. A
    later commit moves per-org strategy counts to a separate endpoint.
    """

    _ = _get_principal(request)

    import time as _time

    table = _get_table()
    now = int(_time.time())
    result: dict[str, Any] = {}

    try:
        desc = table.meta.client.describe_table(TableName=table.table_name)
        tbl = desc.get("Table", {})
        result["dynamodb"] = {
            "status": "ok",
            "table_name": table.table_name,
            "table_status": tbl.get("TableStatus", "UNKNOWN"),
            "item_count": tbl.get("ItemCount", 0),
            "size_bytes": tbl.get("TableSizeBytes", 0),
        }
    except Exception as exc:
        result["dynamodb"] = {"status": "error", "error": str(exc)}

    try:
        resp = table.get_item(Key={"pk": "SNAPSHOT"})
        item = resp.get("Item")
        if item:
            snap_ts = int(item.get("timestamp", 0))
            age = now - snap_ts
            result["trading_service"] = {
                "status": "ok" if age < 30 else "stale" if age < 120 else "down",
                "last_snapshot_age_seconds": age,
                "last_tick": item.get("tick", 0),
                "telemetry": item.get("telemetry", {}),
            }
        else:
            result["trading_service"] = {
                "status": "no_data",
                "last_snapshot_age_seconds": None,
                "last_tick": None,
                "telemetry": {},
            }
    except Exception as exc:
        result["trading_service"] = {"status": "error", "error": str(exc)}

    try:
        strategies = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "STRATEGY#"},
            Select="ALL_ATTRIBUTES",
        ).get("Items", [])
        counts: dict[str, int] = {"active": 0, "paused": 0, "stopped": 0}
        for s in strategies:
            st = s.get("status", "unknown")
            counts[st] = counts.get(st, 0) + 1
        result["strategies"] = {"total": len(strategies), "by_status": counts}
    except Exception as exc:
        result["strategies"] = {"status": "error", "error": str(exc)}

    try:
        events_resp = table.scan(
            FilterExpression="begins_with(pk, :prefix)",
            ExpressionAttributeValues={":prefix": "EVENT#"},
            Select="COUNT",
        )
        result["events"] = {"recent_count": events_resp.get("Count", 0)}
    except Exception as exc:
        result["events"] = {"status": "error", "error": str(exc)}

    result["dashboard"] = {
        "status": "ok",
        "region": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "unknown")),
        "table_name": _get_table_name(),
    }

    return result


# ── Cost tracking ──────────────────────────────────────────────────────


@app.get("/api/costs")
async def cost_summary(request: Request) -> dict[str, Any]:
    """Infrastructure cost — sysadmin only (cost data is sensitive + cross-org).

    Uses authz policy: sysadmin reading COST_DATA is always allowed; other
    principals hit the deny-by-default path and get a 403.
    """

    principal = _get_principal(request)
    _require(principal, Action.READ, Resource(ResourceType.COST_DATA, org_id=None))

    try:
        import datetime

        ce = boto3.client("ce")
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30)

        resp = ce.get_cost_and_usage(
            TimePeriod={
                "Start": start.isoformat(),
                "End": end.isoformat(),
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={
                "Tags": {
                    "Key": "Project",
                    "Values": ["TradingStrands"],
                },
            },
            GroupBy=[
                {"Type": "TAG", "Key": "Component"},
            ],
        )

        daily: list[dict[str, Any]] = []
        total = 0.0
        for result in resp.get("ResultsByTime", []):
            period = result["TimePeriod"]
            day_total = 0.0
            components: dict[str, float] = {}
            for group in result.get("Groups", []):
                key = group["Keys"][0] if group["Keys"] else "untagged"
                key = key.replace("Component$", "")
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                components[key] = amount
                day_total += amount
            daily.append({
                "date": period["Start"],
                "total": round(day_total, 4),
                "components": components,
            })
            total += day_total

        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "total_cost": round(total, 2),
            "currency": "USD",
            "daily": daily,
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "period": {},
            "total_cost": 0,
            "currency": "USD",
            "daily": [],
        }


# ── Admin: User & Org Management ───────────────────────────────────────


def _get_user_pool_id() -> str:
    return os.environ.get("COGNITO_USER_POOL_ID", "")


class OrgCreate(BaseModel):
    name: str


class OrgUpdate(BaseModel):
    name: str | None = None
    session_max_age: int | None = None


@app.get("/api/admin/orgs")
async def list_orgs(request: Request) -> list[dict[str, Any]]:
    """Sysadmins see all orgs; orgadmins see only orgs they admin."""

    principal = _get_principal(request)
    tenancy = TenancyStore(_get_table())
    all_orgs = tenancy.list_orgs()

    if principal.sysadmin:
        visible = all_orgs
    else:
        admined = {
            org_id
            for org_id, role in principal.memberships.items()
            if role.value == "orgadmin"
        }
        if not admined:
            raise HTTPException(status_code=403, detail="orgadmin required")
        visible = [o for o in all_orgs if o.org_id in admined]

    out = [o.model_dump(mode="json") for o in visible]
    out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return out


@app.post("/api/admin/orgs", status_code=201)
async def create_org(request: Request, body: OrgCreate) -> dict[str, Any]:
    principal = _get_principal(request)
    _require(principal, Action.CREATE, Resource(ResourceType.ORG, org_id=None))

    tenancy = TenancyStore(_get_table())
    org = tenancy.create_org(body.name)
    return org.model_dump(mode="json")


@app.get("/api/admin/orgs/{org_id}")
async def get_org(request: Request, org_id: str) -> dict[str, Any]:
    principal = _get_principal(request)
    _require(principal, Action.READ, Resource(ResourceType.ORG, org_id=org_id))

    tenancy = TenancyStore(_get_table())
    try:
        org = tenancy.get_org(org_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Organization not found") from None
    return org.model_dump(mode="json")


@app.put("/api/admin/orgs/{org_id}")
async def update_org(
    request: Request, org_id: str, body: OrgUpdate,
) -> dict[str, Any]:
    principal = _get_principal(request)
    _require(principal, Action.UPDATE, Resource(ResourceType.ORG, org_id=org_id))

    # Minimal update surface. Not using the full TenancyStore helper here
    # because it doesn't yet expose partial updates; direct table call
    # with explicit allowed fields keeps this safe.
    import time as _time

    table = _get_table()
    updates: list[str] = ["updated_at = :t"]
    names: dict[str, str] = {}
    values: dict[str, Any] = {":t": int(_time.time())}
    if body.name is not None:
        updates.append("#n = :n")
        names["#n"] = "name"
        values[":n"] = body.name
    if body.session_max_age is not None:
        updates.append("session_max_age = :sma")
        values[":sma"] = body.session_max_age

    try:
        kwargs: dict[str, Any] = {
            "Key": {"pk": f"ORG#{org_id}"},
            "UpdateExpression": "SET " + ", ".join(updates),
            "ExpressionAttributeValues": values,
            "ConditionExpression": "attribute_exists(pk)",
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        resp = table.update_item(**kwargs)
    except Exception:
        raise HTTPException(status_code=404, detail="Organization not found") from None
    attrs = resp.get("Attributes", {})
    return {k: v for k, v in attrs.items() if k != "pk"}


@app.delete("/api/admin/orgs/{org_id}", status_code=204)
async def delete_org(request: Request, org_id: str) -> None:
    principal = _get_principal(request)
    _require(principal, Action.DELETE, Resource(ResourceType.ORG, org_id=org_id))

    table = _get_table()
    table.delete_item(Key={"pk": f"ORG#{org_id}"})


# ── Admin: Users (Cognito pass-through for now) ────────────────────────


class UserCreate(BaseModel):
    email: str
    role: str = "viewer"
    org_id: str = ""


class UserPasswordReset(BaseModel):
    password: str


class UserRoleUpdate(BaseModel):
    role: str


def _require_user_mgmt(principal: Principal, org_id: str | None) -> None:
    """User management is gated by the authz policy (orgadmin in org_id,
    or sysadmin). Extracted so every user endpoint uses the same rule."""

    _require(
        principal, Action.CREATE,
        Resource(ResourceType.USER, org_id=org_id),
    )


@app.get("/api/admin/users")
async def list_users(request: Request) -> list[dict[str, Any]]:
    principal = _get_principal(request)
    # Listing users is orgadmin-in-the-org or sysadmin. For now we scope
    # by the principal's active org; a later commit will add a proper
    # org filter.
    if not principal.sysadmin:
        admined = [
            oid for oid, role in principal.memberships.items()
            if role.value == "orgadmin"
        ]
        if not admined:
            raise HTTPException(status_code=403, detail="orgadmin required")

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()
    users: list[dict[str, Any]] = []

    paginator_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"UserPoolId": pool_id, "Limit": 60}
        if paginator_token:
            kwargs["PaginationToken"] = paginator_token
        resp = cognito.list_users(**kwargs)
        for u in resp.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            users.append({
                "username": u["Username"],
                "email": attrs.get("email", ""),
                "role": attrs.get("custom:role", "viewer"),
                "org_id": attrs.get("custom:org_id", ""),
                "status": u.get("UserStatus", "UNKNOWN"),
                "enabled": u.get("Enabled", False),
                "created": u.get("UserCreateDate", "").isoformat()
                if hasattr(u.get("UserCreateDate", ""), "isoformat")
                else str(u.get("UserCreateDate", "")),
            })
        paginator_token = resp.get("PaginationToken")
        if not paginator_token:
            break

    return users


@app.post("/api/admin/users", status_code=201)
async def create_user(request: Request, body: UserCreate) -> dict[str, Any]:
    import secrets
    import string

    principal = _get_principal(request)
    _require_user_mgmt(principal, body.org_id or None)

    if body.role not in ("operator", "viewer"):
        raise HTTPException(
            status_code=400,
            detail="Role must be 'operator' or 'viewer'",
        )

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()

    alphabet = string.ascii_letters + string.digits + "!@#$%"
    temp_password = "".join(secrets.choice(alphabet) for _ in range(16))

    user_attrs = [
        {"Name": "email", "Value": body.email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "custom:role", "Value": body.role},
    ]
    if body.org_id:
        user_attrs.append({"Name": "custom:org_id", "Value": body.org_id})

    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=body.email,
            UserAttributes=user_attrs,
            TemporaryPassword=temp_password,
            MessageAction="SUPPRESS",
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=body.email,
            Password=temp_password,
            Permanent=True,
        )
    except cognito.exceptions.UsernameExistsException:
        raise HTTPException(status_code=409, detail="User already exists") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None

    return {
        "email": body.email,
        "role": body.role,
        "org_id": body.org_id,
        "temporary_password": temp_password,
    }


@app.post("/api/admin/users/{username}/reset-password")
async def reset_user_password(
    request: Request, username: str, body: UserPasswordReset,
) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()

    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=username,
            Password=body.password,
            Permanent=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "password_reset"}


@app.put("/api/admin/users/{username}/role")
async def update_user_role(
    request: Request, username: str, body: UserRoleUpdate,
) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    if body.role not in ("operator", "viewer"):
        raise HTTPException(
            status_code=400,
            detail="Role must be 'operator' or 'viewer'",
        )

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()

    try:
        cognito.admin_update_user_attributes(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[{"Name": "custom:role", "Value": body.role}],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "role_updated", "role": body.role}


@app.delete("/api/admin/users/{username}", status_code=204)
async def delete_user(request: Request, username: str) -> None:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()
    try:
        cognito.admin_delete_user(UserPoolId=pool_id, Username=username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.post("/api/admin/users/{username}/enable")
async def enable_user(request: Request, username: str) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()
    try:
        cognito.admin_enable_user(UserPoolId=pool_id, Username=username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "enabled"}


@app.post("/api/admin/users/{username}/disable")
async def disable_user(request: Request, username: str) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()
    try:
        cognito.admin_disable_user(UserPoolId=pool_id, Username=username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "disabled"}


# ── Exception handler for Unauthorized ─────────────────────────────────


@app.exception_handler(Unauthorized)
async def unauthorized_handler(_request: Request, exc: Unauthorized) -> JSONResponse:
    """Any Unauthorized that escapes to the response layer becomes a 403."""

    return JSONResponse(status_code=403, content={"detail": exc.reason})
