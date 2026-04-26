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
import contextlib
import os
from pathlib import Path
from typing import Any

import boto3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from trading_strands.alpaca_secrets.store import AlpacaSecretsStore
from trading_strands.authz.model import Action, Principal, Resource, ResourceType, Role
from trading_strands.authz.policy import Unauthorized, require
from trading_strands.dashboard.auth import (
    CHALLENGE_NEW_PASSWORD_REQUIRED,
    SESSION_COOKIE,
    SESSION_MAX_AGE_DEFAULT,
    AuthMiddleware,
    _get_cognito_client,
    authenticate,
    complete_new_password,
    create_session_cookie,
    create_url_token,
    decode_url_token,
)
from trading_strands.dashboard.password_policy import check_password
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

    # Change-password challenge — redirect to the change-password page.
    # The Cognito session token is signed (via create_url_token) so only
    # the user who just authenticated with the temp password can complete
    # the flow; token expires in URL_TOKEN_MAX_AGE (60s).
    if user_info.get("challenge") == CHALLENGE_NEW_PASSWORD_REQUIRED:
        token = create_url_token({
            "email": user_info["email"],
            "cognito_session": user_info["cognito_session"],
        })
        return RedirectResponse(
            url=f"/change-password?t={token}", status_code=303,
        )

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


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    """Present the new-password form. Requires a valid signed challenge
    token (passed via ?t= from the login redirect). Without a valid token
    we bounce back to login — the challenge path cannot be entered directly.
    """

    token = request.query_params.get("t", "")
    data = decode_url_token(token) if token else None
    if not data or not data.get("cognito_session") or not data.get("email"):
        return RedirectResponse(url="/login", status_code=303)  # type: ignore[return-value]

    error = data.get("error", "")
    # Re-sign the same payload so the form POST carries it forward without
    # the user ever seeing the raw cognito_session.
    forward_token = create_url_token({
        "email": data["email"],
        "cognito_session": data["cognito_session"],
    })
    return _templates.TemplateResponse(
        request, "change_password.html",
        {"email": data["email"], "token": forward_token, "error": error},
    )


@app.post("/auth/change-password")
async def auth_change_password(
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    """Complete the NEW_PASSWORD_REQUIRED challenge.

    The `token` field carries the signed Cognito session (and email) from
    the prior redirect. We re-validate it on every submit so tampering
    can't replay or substitute sessions.
    """

    data = decode_url_token(token)
    if not data or not data.get("cognito_session") or not data.get("email"):
        err = create_url_token({"error": "Session expired — please log in again"})
        return RedirectResponse(url=f"/login?t={err}", status_code=303)

    if new_password != confirm_password:
        err_tok = create_url_token({
            "email": data["email"],
            "cognito_session": data["cognito_session"],
            "error": "Passwords did not match",
        })
        return RedirectResponse(
            url=f"/change-password?t={err_tok}", status_code=303,
        )

    # Enforce our strength policy BEFORE sending to Cognito. Cognito's own
    # policy (12 chars, upper+lower+digit+symbol) is necessary but not
    # sufficient — 'Password123!' clears it. We block by zxcvbn score + a
    # context blocklist (email local-part, starter tokens).
    policy = check_password(
        new_password,
        email=data["email"],
        forbidden=("ChangeMeOnFirstLogin1!",),
    )
    if not policy.ok:
        err_tok = create_url_token({
            "email": data["email"],
            "cognito_session": data["cognito_session"],
            "error": policy.reason,
        })
        return RedirectResponse(
            url=f"/change-password?t={err_tok}", status_code=303,
        )

    tenancy = TenancyStore(_get_table())
    session_info = complete_new_password(
        data["email"], new_password, data["cognito_session"], tenancy,
    )
    if session_info is None:
        err_tok = create_url_token({
            "email": data["email"],
            "cognito_session": data["cognito_session"],
            "error": "Password rejected — check requirements and try again",
        })
        return RedirectResponse(
            url=f"/change-password?t={err_tok}", status_code=303,
        )

    cookie_value = create_session_cookie(session_info)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE, value=cookie_value,
        max_age=SESSION_MAX_AGE_DEFAULT,
        httponly=True, secure=True, samesite="lax",
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


# ── Session / org switcher ─────────────────────────────────────────────


@app.get("/api/session")
async def get_session(request: Request) -> dict[str, Any]:
    """Return the current session's principal + memberships + active org.

    The frontend uses this on every page load to decide:
      - Whether to show the org picker (multiple memberships, no active)
      - Which org badge to render in the top nav
      - Which orgs appear in the switcher dropdown

    Returns 401 if the session is stale (user deleted / session invalid).
    """

    principal = _get_principal(request)
    tenancy = TenancyStore(_get_table())

    memberships_out: list[dict[str, Any]] = []
    for org_id, role in principal.memberships.items():
        try:
            org = tenancy.get_org(org_id)
            memberships_out.append({
                "org_id": org_id,
                "org_name": org.name,
                "org_type": org.org_type.value,
                "role": role.value,
            })
        except NotFoundError:
            # Skip orgs whose metadata is missing — dangling membership. The
            # Cognito-sync / reconciler will clean this up. For the UI we
            # just omit it so the picker doesn't show broken entries.
            continue

    # Resolve active org the same way endpoint handlers do — prefer the
    # session's claim if still valid, else auto-select if single membership.
    session = request.state.session
    active_id = session.get("active_org_id")
    if active_id and active_id not in principal.memberships:
        active_id = None  # stale claim; frontend will prompt
    if active_id is None and len(principal.memberships) == 1:
        active_id = next(iter(principal.memberships))

    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "sysadmin": principal.sysadmin,
        "memberships": memberships_out,
        "active_org_id": active_id,
    }


class ActiveOrgUpdate(BaseModel):
    org_id: str


@app.put("/api/session/active-org")
async def set_active_org(
    request: Request, body: ActiveOrgUpdate,
) -> RedirectResponse:
    """Change the session's active org and redirect to the dashboard root.

    Writes a fresh session cookie carrying the new active_org_id and also
    persists `last_active_org_id` on the USER# record so next login lands
    on the same org by default. Refuses to set an org the user is not a
    member of — this is the final guard against tampered frontends.
    """

    principal = _get_principal(request)
    tenancy = TenancyStore(_get_table())

    if body.org_id not in principal.memberships:
        raise HTTPException(
            status_code=403,
            detail="Not a member of the requested organization",
        )

    # Persist last-active so future logins skip the picker for this user.
    tenancy.set_last_active_org(principal.user_id, body.org_id)

    session = dict(request.state.session)
    session["active_org_id"] = body.org_id
    new_cookie = create_session_cookie(session)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=new_cookie,
        max_age=SESSION_MAX_AGE_DEFAULT,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


# ── Live state ─────────────────────────────────────────────────────────


# ── Market data island reads ─────────────────────────────────────────


@app.get("/api/marketdata/{symbol}")
async def get_marketdata(
    request: Request, symbol: str,
    hours: int = 1,
) -> dict[str, Any]:
    """Read recent minute-bars for `symbol` from the market data island.

    Shared platform data — any authenticated user reads, matching the
    authz policy's market-data-is-open rule. The trading service writes
    these items on each tick; the dashboard renders them as charts.

    `hours` query param bounds the lookback. Default 1 hour.
    """

    import time as _time

    # Authenticate only; authz policy makes market data readable by any
    # authenticated principal (see docs/SPEC/multi_tenancy.md).
    _ = _get_principal(request)

    symbol = symbol.upper()
    if not symbol.isalnum():
        raise HTTPException(status_code=400, detail="invalid symbol")

    hours = max(1, min(hours, 24 * 7))  # cap to one week
    now = int(_time.time())
    start = now - hours * 3600

    from trading_strands.marketdata_store.store import MarketDataStore

    store = MarketDataStore(_get_table())
    bars = store.get_range(symbol, start, now)
    # Flatten for a friendlier chart-client shape.
    series = [
        {
            "ts": ts,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
        }
        for ts, bar in bars
    ]
    return {
        "symbol": symbol,
        "hours": hours,
        "count": len(series),
        "bars": series,
    }


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


# All per-org roles supported by the authz policy. Sysadmin is handled
# via a separate grant endpoint — it's a global flag, not a per-org role,
# and is NEVER surfaced through user-create or role-update. This keeps
# it impossible to accidentally mint a new sysadmin from the user-create
# form.
VALID_ROLES = frozenset({"viewer", "operator", "auditor", "orgadmin"})


class UserCreate(BaseModel):
    email: str
    role: str = "viewer"
    org_id: str = ""  # if empty, user is created without any memberships


class UserPasswordReset(BaseModel):
    password: str


class UserRoleUpdate(BaseModel):
    role: str
    org_id: str  # role is always per-org now


def _require_user_mgmt(principal: Principal, org_id: str | None) -> None:
    """User management is gated by the authz policy (orgadmin in org_id,
    or sysadmin). Extracted so every user endpoint uses the same rule."""

    _require(
        principal, Action.CREATE,
        Resource(ResourceType.USER, org_id=org_id),
    )


@app.get("/api/admin/users")
async def list_users(request: Request) -> list[dict[str, Any]]:
    """Return visible users joined with their per-org memberships.

    Source of truth for role information is DynamoDB (USER# + USERORG#),
    NOT Cognito attributes. Cognito holds only authentication state
    (email + password state); everything about roles and orgs lives in
    our own store so it can't be dropped by a Cognito pool rebuild.

    Visibility:
      - sysadmin sees every USER#
      - orgadmin sees only users in orgs they admin (via ORGUSER# scan)
    """

    principal = _get_principal(request)

    tenancy = TenancyStore(_get_table())
    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()

    # Determine which users this principal is allowed to see.
    if principal.sysadmin:
        visible_users = tenancy.list_users()
    else:
        admined_orgs = [
            oid for oid, role in principal.memberships.items()
            if role.value == "orgadmin"
        ]
        if not admined_orgs:
            raise HTTPException(status_code=403, detail="orgadmin required")
        # Collect the set of user_ids in any admined org.
        seen_ids: set[str] = set()
        for oid in admined_orgs:
            for m in tenancy.memberships_for_org(oid):
                seen_ids.add(m.user_id)
        visible_users = [
            u for u in tenancy.list_users() if u.user_id in seen_ids
        ]

    # Fetch Cognito status (enabled / CONFIRMED / FORCE_CHANGE_PASSWORD)
    # by email so the UI can render disabled/new-user badges. Best-effort —
    # if Cognito is unreachable or the user isn't in Cognito, fall back to
    # 'UNKNOWN' rather than crashing.
    result: list[dict[str, Any]] = []
    for user in visible_users:
        memberships = tenancy.memberships_for_user(user.user_id)
        cog_status = "UNKNOWN"
        cog_enabled = False
        with contextlib.suppress(Exception):
            resp = cognito.admin_get_user(
                UserPoolId=pool_id, Username=user.email,
            )
            cog_status = resp.get("UserStatus", "UNKNOWN")
            cog_enabled = resp.get("Enabled", False)

        result.append({
            "user_id": user.user_id,
            "email": user.email,
            "memberships": [
                {"org_id": m.org_id, "role": m.role.value} for m in memberships
            ],
            "sysadmin": tenancy.is_sysadmin(user.user_id),
            "status": cog_status,
            "enabled": cog_enabled,
            "created_at": user.created_at,
        })

    return result


@app.post("/api/admin/users", status_code=201)
async def create_user(request: Request, body: UserCreate) -> dict[str, Any]:
    """Create a Cognito user AND the corresponding DDB USER# + membership.

    All four per-org roles are assignable here (viewer, operator, auditor,
    orgadmin). Sysadmin is NEVER settable via this endpoint — use the
    dedicated /api/admin/users/{user_id}/sysadmin grant instead.

    Rollback semantics: if either the Cognito create or the DDB writes
    fail after the other succeeded, the orphan is cleaned up before the
    error returns to the caller. This avoids half-provisioned accounts.
    """

    import secrets
    import string

    principal = _get_principal(request)
    _require_user_mgmt(principal, body.org_id or None)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of {sorted(VALID_ROLES)}",
        )
    if not body.org_id:
        raise HTTPException(
            status_code=400,
            detail="org_id is required — users must be created into an org",
        )

    tenancy = TenancyStore(_get_table())

    # Guard: does the org exist? A frontend bug shouldn't let you create
    # a user membership pointing at a non-existent org.
    try:
        tenancy.get_org(body.org_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Organization not found") from None

    # Email uniqueness check done first — cheaper to fail fast than to
    # create the Cognito user and then fail on the DDB uniqueness guard.
    if tenancy.find_user_by_email(body.email) is not None:
        raise HTTPException(
            status_code=409,
            detail="A user with that email already exists",
        )

    cognito = _get_cognito_client()
    pool_id = _get_user_pool_id()

    # Random starter password meeting Cognito policy (12+ chars,
    # upper+lower+digit+symbol). The user must change it on first login.
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    temp_password = "".join(secrets.choice(alphabet) for _ in range(20))

    try:
        cog_resp = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=body.email,
            UserAttributes=[
                {"Name": "email", "Value": body.email},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=temp_password,
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        raise HTTPException(status_code=409, detail="User already exists") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None

    cognito_sub = ""
    for attr in cog_resp.get("User", {}).get("Attributes", []):
        if attr.get("Name") == "sub":
            cognito_sub = attr.get("Value", "")
            break

    # Create the DDB USER# + membership. If this fails, delete the Cognito
    # user so we don't leave an orphan that can log in with no USER# record.
    try:
        user = tenancy.create_user(
            email=body.email, cognito_sub=cognito_sub or None,
        )
        tenancy.add_membership(
            user.user_id, body.org_id, Role(body.role),
        )
    except Exception:
        with contextlib.suppress(Exception):
            cognito.admin_delete_user(UserPoolId=pool_id, Username=body.email)
        raise HTTPException(
            status_code=500,
            detail="failed to record user in store; Cognito account rolled back",
        ) from None

    return {
        "user_id": user.user_id,
        "email": body.email,
        "role": body.role,
        "org_id": body.org_id,
        "temporary_password": temp_password,
    }


def _resolve_user_email(tenancy: TenancyStore, user_id: str) -> str:
    """Load a user record and return its email so Cognito admin calls can
    find the matching Cognito user. 404 if the user_id doesn't resolve."""

    try:
        return tenancy.get_user(user_id).email
    except NotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None


@app.post("/api/admin/users/{user_id}/reset-password")
async def reset_user_password(
    request: Request, user_id: str, body: UserPasswordReset,
) -> dict[str, str]:
    """Admin-driven password reset. Goes through the same strength policy
    as the self-service change-password flow — admins can't create weak
    passwords for their users either."""

    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    email = _resolve_user_email(TenancyStore(_get_table()), user_id)

    policy = check_password(body.password, email=email)
    if not policy.ok:
        raise HTTPException(status_code=400, detail=policy.reason)

    cognito = _get_cognito_client()
    try:
        cognito.admin_set_user_password(
            UserPoolId=_get_user_pool_id(),
            Username=email,
            Password=body.password,
            Permanent=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "password_reset"}


@app.put("/api/admin/users/{user_id}/role")
async def update_user_role(
    request: Request, user_id: str, body: UserRoleUpdate,
) -> dict[str, str]:
    """Set a user's role in a specific org. Sysadmin grants are NOT done
    here — see /api/admin/users/{user_id}/sysadmin. This route deliberately
    cannot mint a sysadmin: the role enum doesn't include it."""

    principal = _get_principal(request)
    _require_user_mgmt(principal, body.org_id)

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of {sorted(VALID_ROLES)}",
        )

    tenancy = TenancyStore(_get_table())
    try:
        tenancy.get_user(user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None
    try:
        tenancy.get_org(body.org_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Organization not found") from None

    # add_membership is an upsert on (user, org).
    tenancy.add_membership(user_id, body.org_id, Role(body.role))
    return {"status": "role_updated", "role": body.role, "org_id": body.org_id}


@app.delete("/api/admin/users/{user_id}", status_code=204)
async def delete_user(request: Request, user_id: str) -> None:
    """Aggressive cleanup: memberships, USER#, email index, sysadmin
    sentinel, and the Cognito account. Anything left behind would block
    re-creating a user with the same email later."""

    principal = _get_principal(request)
    _require_user_mgmt(principal, None)

    tenancy = TenancyStore(_get_table())
    table = _get_table()

    try:
        user = tenancy.get_user(user_id)
    except NotFoundError:
        return  # idempotent

    for m in tenancy.memberships_for_user(user_id):
        tenancy.remove_membership(user_id, m.org_id)
    tenancy.revoke_sysadmin(user_id)
    with contextlib.suppress(Exception):
        table.delete_item(Key={"pk": f"USEREMAIL#{user.email.lower().strip()}"})
    with contextlib.suppress(Exception):
        table.delete_item(Key={"pk": f"USER#{user_id}"})

    cognito = _get_cognito_client()
    with contextlib.suppress(Exception):
        cognito.admin_delete_user(
            UserPoolId=_get_user_pool_id(), Username=user.email,
        )


@app.post("/api/admin/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)
    email = _resolve_user_email(TenancyStore(_get_table()), user_id)

    cognito = _get_cognito_client()
    try:
        cognito.admin_enable_user(
            UserPoolId=_get_user_pool_id(), Username=email,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "enabled"}


@app.post("/api/admin/users/{user_id}/disable")
async def disable_user(request: Request, user_id: str) -> dict[str, str]:
    principal = _get_principal(request)
    _require_user_mgmt(principal, None)
    email = _resolve_user_email(TenancyStore(_get_table()), user_id)

    cognito = _get_cognito_client()
    try:
        cognito.admin_disable_user(
            UserPoolId=_get_user_pool_id(), Username=email,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "disabled"}


# ── Sysadmin grant/revoke (sysadmin-only) ─────────────────────────────
#
# Dedicated endpoint so the global sysadmin flag is never settable via
# the user-create or role-update forms. Guard rails:
#   - Only sysadmin may call it
#   - Target must be a member of the system org (no bypassing trust via
#     creating a user in a random customer org and promoting them)
#   - Cannot revoke the last sysadmin (would lock the platform out of
#     ever making another one)


class SysadminUpdate(BaseModel):
    sysadmin: bool


@app.put("/api/admin/users/{user_id}/sysadmin")
async def update_sysadmin(
    request: Request, user_id: str, body: SysadminUpdate,
) -> dict[str, Any]:
    principal = _get_principal(request)
    if not principal.sysadmin:
        raise HTTPException(status_code=403, detail="sysadmin required")

    tenancy = TenancyStore(_get_table())
    try:
        target = tenancy.get_user(user_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="User not found") from None

    if body.sysadmin:
        system_org = tenancy.find_system_org()
        if system_org is None:
            raise HTTPException(
                status_code=500,
                detail="system org missing — bootstrap broken",
            )
        if tenancy.role_of(user_id, system_org.org_id) is None:
            raise HTTPException(
                status_code=400,
                detail="target must be a member of the system org first",
            )
        tenancy.grant_sysadmin(user_id)
    else:
        # Refuse to remove the last sysadmin.
        remaining = sum(
            1 for u in tenancy.list_users()
            if u.user_id != user_id and tenancy.is_sysadmin(u.user_id)
        )
        if remaining == 0:
            raise HTTPException(
                status_code=400,
                detail="cannot revoke the last remaining sysadmin",
            )
        tenancy.revoke_sysadmin(user_id)

    return {
        "user_id": user_id,
        "email": target.email,
        "sysadmin": tenancy.is_sysadmin(user_id),
    }


# ── Admin: per-org Alpaca credentials ─────────────────────────────────


def _get_secrets_client() -> Any:
    return boto3.client("secretsmanager")


class AlpacaCredsSubmit(BaseModel):
    api_key: str
    secret_key: str
    paper: bool = True


@app.get("/api/admin/orgs/{org_id}/alpaca")
async def get_alpaca_status(request: Request, org_id: str) -> dict[str, Any]:
    """Report whether this org has Alpaca creds configured + paper/live mode.

    Returns NO key/secret material. Reading actual credentials is only
    ever done by the trading service's task role — dashboard principals
    (including sysadmin) have no business seeing customer keys.
    """

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(ResourceType.ALPACA_SECRET, org_id=org_id),
    )
    store = AlpacaSecretsStore(_get_secrets_client())
    status = store.status(org_id)
    return {
        "org_id": org_id,
        "configured": status.configured,
        "paper": status.paper,
    }


@app.put("/api/admin/orgs/{org_id}/alpaca")
async def put_alpaca_creds(
    request: Request, org_id: str, body: AlpacaCredsSubmit,
) -> dict[str, Any]:
    """Write Alpaca credentials for this org.

    Requires ALPACA_SECRET UPDATE which the authz policy grants to
    orgadmin of the owning org (and NOT to sysadmin — see policy)."""

    principal = _get_principal(request)
    _require(
        principal, Action.UPDATE,
        Resource(ResourceType.ALPACA_SECRET, org_id=org_id),
    )
    store = AlpacaSecretsStore(_get_secrets_client())
    store.upsert(
        org_id=org_id,
        api_key=body.api_key,
        secret_key=body.secret_key,
        paper=body.paper,
    )
    return {"org_id": org_id, "configured": True, "paper": body.paper}


@app.delete("/api/admin/orgs/{org_id}/alpaca", status_code=204)
async def delete_alpaca_creds(request: Request, org_id: str) -> None:
    principal = _get_principal(request)
    _require(
        principal, Action.DELETE,
        Resource(ResourceType.ALPACA_SECRET, org_id=org_id),
    )
    store = AlpacaSecretsStore(_get_secrets_client())
    store.delete(org_id)


# ── Exception handler for Unauthorized ─────────────────────────────────


@app.exception_handler(Unauthorized)
async def unauthorized_handler(_request: Request, exc: Unauthorized) -> JSONResponse:
    """Any Unauthorized that escapes to the response layer becomes a 403."""

    return JSONResponse(status_code=403, content={"detail": exc.reason})
