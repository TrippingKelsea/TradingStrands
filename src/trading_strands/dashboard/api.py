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


def _active_org_or_empty(request: Request) -> str:
    """Lenient variant of _get_active_org for callers (like /api/halt)
    where an unset active org is not an error on its own. Returns empty
    string when nothing's claimed; does no membership check — callers
    decide what to do."""

    claimed = request.query_params.get("org") or (
        request.state.session.get("active_org_id")
        if hasattr(request.state, "session") else None
    )
    return str(claimed or "")


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
    info = ""
    token = request.query_params.get("t", "")
    if token:
        data = decode_url_token(token)
        if data:
            error = data.get("error", "")
    # Set by the dashboard's fetch interceptor when an API call returned
    # 401 — the session expired while the user was logged in.
    if request.query_params.get("expired"):
        info = "Your session expired. Please sign in again."
    return _templates.TemplateResponse(
        request, "login.html", {"error": error, "info": info},
    )


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


# ── Token usage telemetry reads ───────────────────────────────────────


@app.get("/api/tokens/today")
async def tokens_today(request: Request) -> dict[str, Any]:
    """Return today's token + cost totals for the principal's active org
    plus per-agent breakdown. Used by the dashboard token-usage widget.

    Authz: any org member can read their own org's totals (tokens are
    cost the user should see). Sysadmin with SYSADMIN_CAN_READ_ORG_DATA
    can query any org via ?org= param.
    """

    import time as _time

    principal = _get_principal(request)
    org_id = _get_active_org(request, principal)

    from trading_strands.token_telemetry.store import TokenUsageStore, _date_key

    store = TokenUsageStore(_get_table())
    date = _date_key(int(_time.time()))

    org_total = store.daily_org_total(org_id, date)
    # Scan for the per-agent rows (pk begins with TOKEN#{org_id}#...#{date},
    # where the middle segment is the agent_id).
    table = _get_table()
    resp = table.scan(
        FilterExpression="begins_with(pk, :p) AND #d = :d",
        ExpressionAttributeNames={"#d": "date"},
        ExpressionAttributeValues={
            ":p": f"TOKEN#{org_id}#",
            ":d": date,
        },
    )
    per_agent: list[dict[str, Any]] = []
    for item in resp.get("Items", []):
        # Skip the org-summary row (which has no agent_id).
        if not item.get("agent_id"):
            continue
        per_agent.append({
            "agent_id": item.get("agent_id"),
            "agent_type": item.get("agent_type", "unknown"),
            "input_tokens": int(item.get("input_tokens", 0)),
            "output_tokens": int(item.get("output_tokens", 0)),
            "invocations": int(item.get("invocations", 0)),
            "cost_usd_est": str(item.get("cost_usd_est", "0")),
        })
    per_agent.sort(key=lambda x: float(x["cost_usd_est"]), reverse=True)

    return {
        "org_id": org_id,
        "date": date,
        "total": {
            "input_tokens": int(org_total.get("input_tokens", 0)),
            "output_tokens": int(org_total.get("output_tokens", 0)),
            "invocations": int(org_total.get("invocations", 0)),
            "cost_usd_est": str(org_total.get("cost_usd_est", "0")),
        },
        "per_agent": per_agent,
    }


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


# ── Model allowlist (for strategy edit UI dropdown) ────────────────────


@app.get("/api/models")
async def list_models(request: Request) -> list[dict[str, str]]:
    """Return the allowlisted models a strategy author can choose from.

    Gated on authenticated session only — the list itself is not org-
    scoped. UI renders this as the <select> in the strategy form so the
    dropdown options never drift from `validate_model_id` on save.
    """

    _ = _get_principal(request)
    from trading_strands.models.registry import DEFAULT_MODEL_ID, available_models
    return [
        {
            "id": m.id,
            "label": m.label,
            "provider": m.provider,
            "description": m.description,
            "is_default": "true" if m.id == DEFAULT_MODEL_ID else "false",
        }
        for m in available_models()
    ]


# ── Strategy CRUD (org-scoped) ─────────────────────────────────────────


class StrategyCreate(BaseModel):
    name: str
    markdown: str
    symbols: list[str] = []
    capital: str = "1000"
    # Optional from day one — strategies without tools/skills are
    # the v0 default.
    tools: dict[str, dict[str, Any]] = {}
    skills: list[str] = []
    # Empty string = platform default model. Allowlist-validated at
    # the store; surfaces as 400 here on unknown ids.
    model_id: str = ""


class StrategyUpdate(BaseModel):
    name: str | None = None
    markdown: str | None = None
    symbols: list[str] | None = None
    capital: str | None = None
    status: str | None = None
    tools: dict[str, dict[str, Any]] | None = None
    skills: list[str] | None = None
    model_id: str | None = None


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

    from trading_strands.models.registry import UnknownModelError
    from trading_strands.tools.base import StrategyToolConfig

    store = StrategyStore(_get_table())
    try:
        strat = store.create(
            org_id=org_id,
            author_user_id=principal.user_id,
            name=body.name,
            markdown=body.markdown,
            symbols=body.symbols,
            capital=body.capital,
            tools={
                name: StrategyToolConfig(**cfg)
                for name, cfg in body.tools.items()
            },
            skills=body.skills,
            model_id=body.model_id,
        )
    except UnknownModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    if body.tools is not None:
        # Stored as plain dict inside the Strategy row so the existing
        # JSON-dump path handles it.
        update_fields["tools"] = body.tools
    if body.skills is not None:
        update_fields["skills"] = body.skills
    if body.model_id is not None:
        update_fields["model_id"] = body.model_id

    from trading_strands.models.registry import UnknownModelError
    try:
        updated = store.update(strategy_id, update_fields)
    except UnknownModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


_ecs_client_cache: Any = None


def _get_ecs_client() -> Any:
    """Lazily-constructed ECS client. Kept behind a helper so tests can
    patch it without plumbing dependency injection through every
    endpoint."""

    global _ecs_client_cache
    if _ecs_client_cache is None:
        _ecs_client_cache = boto3.client("ecs")
    return _ecs_client_cache


def _parse_td_revision(td_arn: str) -> int | None:
    """Pull the ':<N>' revision off a task-definition ARN.

    ARN shape: arn:aws:ecs:REGION:ACCT:task-definition/FAMILY:REVISION
    """

    if ":" not in td_arn:
        return None
    tail = td_arn.rsplit(":", 1)[-1]
    if not tail.isdigit():
        return None
    return int(tail)


@app.get("/api/strategies/{strategy_id}/service")
async def get_strategy_service(
    request: Request, strategy_id: str,
) -> dict[str, Any]:
    """Report what the StrategySupervisor has set up in ECS for this
    strategy. Operators use this to see whether cutover actually
    landed a per-bot service and what state it's in."""

    from trading_strands.supervisor.strategy_supervisor import (
        service_name_for,
    )

    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy not found") from None

    acl = store.acl_users(strategy_id)
    _require(principal, Action.READ, resource_for(strat, acl))

    cluster = os.environ.get("ECS_CLUSTER")
    if not cluster:
        raise HTTPException(
            status_code=503,
            detail=(
                "Per-bot Fargate not configured (ECS_CLUSTER unset). "
                "This dashboard cannot report service state."
            ),
        )

    service_name = service_name_for(strategy_id)
    ecs = _get_ecs_client()
    resp = ecs.describe_services(cluster=cluster, services=[service_name])
    services = resp.get("services", [])
    if not services or services[0].get("status") in ("MISSING", "INACTIVE"):
        return {
            "exists": False,
            "service_name": service_name,
            "status": None,
            "desired_count": 0,
            "running_count": 0,
            "pending_count": 0,
            "task_definition_revision": None,
        }

    svc = services[0]
    return {
        "exists": True,
        "service_name": service_name,
        "status": svc.get("status"),
        "desired_count": int(svc.get("desiredCount", 0)),
        "running_count": int(svc.get("runningCount", 0)),
        "pending_count": int(svc.get("pendingCount", 0)),
        "task_definition_revision": _parse_td_revision(
            str(svc.get("taskDefinition", "")),
        ),
    }


# Whitelist of review-agent types the dashboard will read recommendations
# for. Set-membership check also neutralizes path-traversal attempts like
# "..%2Fsomething" — any value not in this set returns 400 without ever
# touching S3.
_ALLOWED_RECOMMENDATION_AGENT_TYPES = frozenset({
    "risk", "compliance", "auditor",
})


@app.get("/api/orgs/{org_id}/heartbeats/review")
async def get_org_review_heartbeats(
    request: Request, org_id: str,
) -> dict[str, Any]:
    """Return the last-beat timestamp for each review agent (risk,
    compliance, auditor) scoped to an org. null when an agent has
    never run for this org (i.e. heartbeat row doesn't exist).

    Authz: READ on the Org resource — same gate as recommendations,
    since heartbeat existence leaks no more than the recommendation
    body does.
    """

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(type=ResourceType.ORG, org_id=org_id),
    )

    table = _get_table()
    result: dict[str, int | None] = {
        "risk": None, "compliance": None, "auditor": None,
    }
    for agent_type in result:
        resp = table.get_item(
            Key={"pk": f"HEARTBEAT#{agent_type}#{org_id}"},
        )
        item = resp.get("Item")
        if item is not None:
            ts = item.get("last_beat_ts")
            if ts is not None:
                result[agent_type] = int(ts)
    return result


class OrgToolToggle(BaseModel):
    enabled: bool


@app.get("/api/orgs/{org_id}/tools")
async def list_org_tools(
    request: Request, org_id: str,
) -> list[dict[str, Any]]:
    """List per-org tool availability rows. Any org member can read
    (same gate as /recommendations)."""

    from trading_strands.org_tools.store import OrgToolsStore

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(type=ResourceType.ORG, org_id=org_id),
    )
    store = OrgToolsStore(_get_table())
    configs = store.list_for_org(org_id)
    return [c.model_dump(mode="json") for c in configs]


@app.put("/api/orgs/{org_id}/tools/{tool_name}")
async def set_org_tool(
    request: Request, org_id: str, tool_name: str, body: OrgToolToggle,
) -> dict[str, Any]:
    """Orgadmin toggles the org-level availability of a tool. Per
    SPEC §5.4: disabling takes the tool away from strategies on
    next bot restart; enabling makes it available to strategies
    that opt in."""

    from trading_strands.org_tools.store import OrgToolsStore

    principal = _get_principal(request)
    # Orgadmin-only — same bar as the per-org Alpaca cred editor.
    role = principal.memberships.get(org_id)
    if not principal.sysadmin and (role is None or role.value != "orgadmin"):
        raise HTTPException(
            status_code=403,
            detail=f"org tool config requires orgadmin of {org_id}",
        )
    store = OrgToolsStore(_get_table())
    cfg = store.set_enabled(
        org_id=org_id,
        tool_name=tool_name,
        enabled=body.enabled,
        updated_by=principal.user_id,
    )
    return cfg.model_dump(mode="json")


class SkillBody(BaseModel):
    markdown: str


@app.get("/api/orgs/{org_id}/skills")
async def list_org_skills(
    request: Request, org_id: str,
) -> list[dict[str, Any]]:
    from trading_strands.skills_store.store import SkillsStore

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(type=ResourceType.ORG, org_id=org_id),
    )
    store = SkillsStore(_get_table())
    return [s.model_dump(mode="json") for s in store.list_for_org(org_id)]


@app.get("/api/orgs/{org_id}/skills/{skill_name}")
async def get_org_skill(
    request: Request, org_id: str, skill_name: str,
) -> dict[str, Any]:
    from trading_strands.skills_store.store import (
        SkillNotFoundError,
        SkillsStore,
    )

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(type=ResourceType.ORG, org_id=org_id),
    )
    try:
        skill = SkillsStore(_get_table()).get(org_id, skill_name)
    except SkillNotFoundError:
        raise HTTPException(status_code=404, detail="Skill not found") from None
    return skill.model_dump(mode="json")


@app.put("/api/orgs/{org_id}/skills/{skill_name}")
async def put_org_skill(
    request: Request, org_id: str, skill_name: str, body: SkillBody,
) -> dict[str, Any]:
    """Create or update a skill. Orgadmin-only."""

    from trading_strands.skills_store.store import (
        SkillsStore,
        SkillTooLargeError,
    )

    principal = _get_principal(request)
    role = principal.memberships.get(org_id)
    if not principal.sysadmin and (role is None or role.value != "orgadmin"):
        raise HTTPException(
            status_code=403,
            detail=f"editing skills requires orgadmin of {org_id}",
        )
    try:
        skill = SkillsStore(_get_table()).put(
            org_id=org_id,
            skill_name=skill_name,
            markdown=body.markdown,
            author_user_id=principal.user_id,
        )
    except SkillTooLargeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return skill.model_dump(mode="json")


@app.delete("/api/orgs/{org_id}/skills/{skill_name}", status_code=204)
async def delete_org_skill(
    request: Request, org_id: str, skill_name: str,
) -> None:
    """Orgadmin delete. Idempotent — deleting a missing skill is
    a no-op, matching the store semantics."""

    from trading_strands.skills_store.store import SkillsStore

    principal = _get_principal(request)
    role = principal.memberships.get(org_id)
    if not principal.sysadmin and (role is None or role.value != "orgadmin"):
        raise HTTPException(
            status_code=403,
            detail=f"deleting skills requires orgadmin of {org_id}",
        )
    SkillsStore(_get_table()).delete(org_id, skill_name)


@app.get("/api/orgs/{org_id}/recommendations/{agent_type}")
async def get_org_recommendations(
    request: Request, org_id: str, agent_type: str,
) -> dict[str, Any]:
    """Return recommendations.md for a review agent scoped to an org.

    Authz: READ on the Org resource — any member of the org passes.
    Non-members 403 regardless of what the bucket would have returned.
    """

    if agent_type not in _ALLOWED_RECOMMENDATION_AGENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "agent_type must be one of: "
                f"{sorted(_ALLOWED_RECOMMENDATION_AGENT_TYPES)}"
            ),
        )

    principal = _get_principal(request)
    _require(
        principal, Action.READ,
        Resource(type=ResourceType.ORG, org_id=org_id),
    )

    bucket = os.environ.get("AGENT_MEMORY_BUCKET")
    if not bucket:
        raise HTTPException(
            status_code=503,
            detail=(
                "Agent memory not configured (AGENT_MEMORY_BUCKET unset). "
                "Review-agent recommendations unavailable."
            ),
        )

    from trading_strands.agent_memory.store import AgentMemoryStore

    # Review agents use agent_id == org_id by convention — one memory
    # bucket per (org, agent_type). See each agent's lambda_handler.
    memory = AgentMemoryStore(
        s3_client=boto3.client("s3"),
        bucket=bucket,
        org_id=org_id,
        agent_type=agent_type,
        agent_id=org_id,
    )
    return {
        "org_id": org_id,
        "agent_type": agent_type,
        "recommendations": memory.read_recommendations(),
    }


@app.get("/api/strategies/{strategy_id}/lessons")
async def get_strategy_lessons(
    request: Request, strategy_id: str,
) -> dict[str, Any]:
    """Return the self-critique lessons.md for this strategy.

    Lessons contain the critique agent's reasoning — same privacy
    boundary as the strategy itself: READ on the strategy = READ on
    the lessons. Missing lessons file returns empty string (a new
    strategy with no critiques yet).
    """

    principal = _get_principal(request)
    store = StrategyStore(_get_table())
    try:
        strat = store.get(strategy_id)
    except StrategyNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy not found") from None

    acl = store.acl_users(strategy_id)
    _require(principal, Action.READ, resource_for(strat, acl))

    bucket = os.environ.get("AGENT_MEMORY_BUCKET")
    if not bucket:
        raise HTTPException(
            status_code=503,
            detail=(
                "Agent memory not configured (AGENT_MEMORY_BUCKET unset). "
                "Self-critique lessons unavailable."
            ),
        )

    from trading_strands.agent_memory.store import AgentMemoryStore

    memory = AgentMemoryStore(
        s3_client=boto3.client("s3"),
        bucket=bucket,
        org_id=strat.org_id,
        agent_type="strategy",
        agent_id=f"strategy-{strategy_id}",
    )
    return {
        "strategy_id": strategy_id,
        "lessons": memory.read_lessons(),
    }


# ── Halt control ───────────────────────────────────────────────────────


class HaltRequest(BaseModel):
    """Halt scope + optional reason. Omit scope for back-compat: callers
    with sysadmin get system halt, orgadmins get their active-org halt."""

    scope: str | None = None  # "system" | "org"
    org_id: str | None = None
    reason: str | None = None


def _halt_scope_and_org(
    principal: Principal, body: HaltRequest, active_org_id: str,
) -> tuple[str, str | None]:
    """Resolve (scope, org_id) for a halt/unhalt request with authz.

    Rules:
      - Explicit scope=system: sysadmin only.
      - Explicit scope=org, org_id=X: the caller must be orgadmin of X
        (or sysadmin).
      - No explicit scope:
          - sysadmin → system halt (broadest power they have)
          - orgadmin of active org → org halt of active org
          - anyone else → 403
    """

    if body.scope == "system":
        if not principal.sysadmin:
            raise HTTPException(
                status_code=403,
                detail="system halt requires sysadmin",
            )
        return "system", None

    if body.scope == "org":
        target = body.org_id or active_org_id
        if not target:
            raise HTTPException(
                status_code=400, detail="org halt requires org_id",
            )
        is_admin = principal.memberships.get(target)
        if not principal.sysadmin and (
            is_admin is None or is_admin.value != "orgadmin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"org halt requires orgadmin of {target}",
            )
        return "org", target

    # No explicit scope — pick the narrowest the caller is entitled to.
    if principal.sysadmin:
        return "system", None
    if any(r.value == "orgadmin" for r in principal.memberships.values()):
        # Prefer the active org; fall back to the caller's single
        # orgadmin membership when nothing's active.
        resolved: str
        if active_org_id and principal.memberships.get(active_org_id):
            resolved = active_org_id
        else:
            admin_orgs = [
                oid for oid, r in principal.memberships.items()
                if r.value == "orgadmin"
            ]
            if len(admin_orgs) != 1:
                raise HTTPException(
                    status_code=400,
                    detail="specify org_id — caller admins multiple orgs",
                )
            resolved = admin_orgs[0]
        return "org", resolved

    raise HTTPException(
        status_code=403,
        detail="halt requires orgadmin or sysadmin",
    )


@app.get("/api/halt/events")
async def get_halt_events(
    request: Request, limit: int = 50,
) -> dict[str, Any]:
    """Recent halt/unhalt transitions, newest first. Backs the
    dashboard's halt-history view."""

    from trading_strands.halt.store import HaltStore

    _ = _get_principal(request)
    if limit <= 0 or limit > 200:
        raise HTTPException(
            status_code=400, detail="limit must be between 1 and 200",
        )
    events = HaltStore(_get_table()).list_events(limit=limit)
    return {
        "events": [
            {
                "ts": e.ts,
                "scope": e.scope,
                "halted": e.halted,
                "reason": e.reason,
                "org_id": e.org_id,
            }
            for e in events
        ],
    }


@app.get("/api/halt")
async def get_halt_state(request: Request) -> dict[str, Any]:
    """Return halt state + caller's permission flags.

    - `system`: always included — a sysadmin emergency stop affects
      every user and must be visible regardless of org membership.
    - `org`: included for the active org if the caller is a member;
      `None` otherwise (e.g. sysadmin with no active org selected).
    - `effective_halted`: the OR-view the Coordinator enforces, scoped
      to the active org (or system-only if no active org).
    - `can_halt_system` / `can_halt_org`: permission hints for the UI
      so it can enable/disable scope options without guessing.
    """

    from trading_strands.halt.store import HaltStore

    principal = _get_principal(request)
    active = _active_org_or_empty(request)

    hs = HaltStore(_get_table())
    sys_state = hs.get_system_state()
    system_view = {
        "halted": sys_state.halted,
        "reason": sys_state.reason,
        "updated_at": sys_state.updated_at,
    }

    org_view: dict[str, Any] | None = None
    if active and active in principal.memberships:
        org_state = hs.get_org_state(active)
        org_view = {
            "halted": org_state.halted,
            "reason": org_state.reason,
            "updated_at": org_state.updated_at,
            "org_id": active,
        }

    effective = sys_state.halted or bool(org_view and org_view["halted"])

    can_halt_system = principal.sysadmin
    can_halt_org = (
        principal.sysadmin
        or (active and principal.memberships.get(active)
            and principal.memberships[active].value == "orgadmin")
    )

    return {
        "system": system_view,
        "org": org_view,
        "effective_halted": effective,
        "can_halt_system": bool(can_halt_system),
        "can_halt_org": bool(can_halt_org),
    }


@app.post("/api/halt")
async def halt_trading(
    request: Request, body: HaltRequest | None = None,
) -> dict[str, str]:
    """Halt trading. Scope depends on caller + request body; see
    _halt_scope_and_org for the resolution rules.

    Sysadmin halt (scope=system) stops every org regardless of per-org
    state. Orgadmin halt (scope=org) stops one org only — sibling orgs
    keep running."""

    from trading_strands.halt.store import HaltStore

    principal = _get_principal(request)
    active = _active_org_or_empty(request)
    body = body or HaltRequest()
    scope, org_id = _halt_scope_and_org(principal, body, active)
    reason = body.reason or f"dashboard halt by {principal.email}"

    hs = HaltStore(_get_table())
    if scope == "system":
        hs.set_system_halt(True, reason=reason)
        return {"status": "halted", "scope": "system"}
    assert org_id is not None  # _halt_scope_and_org post-condition
    hs.set_org_halt(org_id, True, reason=reason)
    return {"status": "halted", "scope": "org", "org_id": org_id}


@app.post("/api/unhalt")
async def unhalt_trading(
    request: Request, body: HaltRequest | None = None,
) -> dict[str, str]:
    from trading_strands.halt.store import HaltStore

    principal = _get_principal(request)
    active = _active_org_or_empty(request)
    body = body or HaltRequest()
    scope, org_id = _halt_scope_and_org(principal, body, active)
    reason = body.reason or f"dashboard unhalt by {principal.email}"

    hs = HaltStore(_get_table())
    if scope == "system":
        hs.set_system_halt(False, reason=reason)
        return {"status": "running", "scope": "system"}
    assert org_id is not None
    hs.set_org_halt(org_id, False, reason=reason)
    return {"status": "running", "scope": "org", "org_id": org_id}


# ── Platform Supervisor (external agent-health view) ───────────────────


_cloudwatch_client_cache: Any = None

# Alarms defined by this stack's CDK. Kept as an explicit allowlist so
# unrelated account-level alarms (other teams, other stacks) don't leak
# into the dashboard's supervisor panel.
_TRADING_STRANDS_ALARM_NAMES = frozenset({
    "trading-strands-system-halt",
    "trading-strands-org-halt",
    "trading-strands-missing-agents",
})


def _get_cloudwatch_client() -> Any:
    """Lazily construct the CW client. Cached so tests can patch this
    helper rather than boto3 globally."""

    global _cloudwatch_client_cache
    if _cloudwatch_client_cache is None:
        _cloudwatch_client_cache = boto3.client("cloudwatch")
    return _cloudwatch_client_cache


# ── Deploy markers ────────────────────────────────────────────────────
#
# The dashboard overlays vertical dashed lines at deploy times so a
# latency regression can be correlated with the commit that caused
# it. The source of truth for deploys is the GitHub Actions run log,
# which the dashboard pod doesn't have direct access to; instead we
# stash the build's commit SHA + deploy time in an env var at image
# build time (`DEPLOY_COMMIT`, `DEPLOY_TIMESTAMP`) and surface that.
# Multiple deploy markers will come online once the CI pipeline
# writes a DEPLOY#<ts> DDB row — handled in a follow-up commit.


@app.get("/api/deploys/recent")
async def deploys_recent(request: Request) -> dict[str, Any]:
    """Return recent deploy markers for chart overlays.

    v0: one synthetic marker per image build from DEPLOY_COMMIT +
    DEPLOY_TIMESTAMP env vars. v1 reads DEPLOY#<ts> rows from DDB.
    Empty list is a valid response — the client renders charts
    without markers rather than erroring out.
    """

    _ = _get_principal(request)

    import time as _time

    deploys: list[dict[str, Any]] = []
    ts_env = os.environ.get("DEPLOY_TIMESTAMP", "")
    sha = os.environ.get("DEPLOY_COMMIT", "")
    if ts_env:
        try:
            ts = int(ts_env)
            if ts <= 0 or ts > int(_time.time()) + 3600:
                raise ValueError("unreasonable deploy timestamp")
            deploys.append({"ts": ts, "commit": sha[:7] if sha else ""})
        except ValueError:
            pass
    return {"deploys": deploys}


# ── Generic metric query ──────────────────────────────────────────────
#
# `/api/metrics/query` wraps CloudWatch GetMetricData behind a single
# endpoint so the dashboard can render arbitrary EMF-backed panels
# (decision latency, broker intent throughput, token cost, …) without
# a bespoke handler per panel. The endpoint is allowlist-gated on
# metric name + namespace so the UI can't accidentally pull account-
# wide metrics or another team's namespace. Dimension values are
# passed through verbatim — callers are already authenticated and
# dimension filtering is the client's job.


class MetricsQueryRequest(BaseModel):
    namespace: str = "TradingStrands"
    metric_name: str
    # {name: value} equality filters. Empty dict = any dimensions.
    dimensions: dict[str, str] = {}
    # 60/300/3600. Default 60s for fine-grained dashboards.
    period_seconds: int = 60
    # Lookback. Default 1h. Cap is 14d — CloudWatch GetMetricData
    # supports longer but the UI widgets we have don't.
    lookback_seconds: int = 3600
    # Average is default for latency; Sum for counts.
    stat: str = "Average"


# Names the dashboard is allowed to query. Bounded so a browser
# bug / malicious script can't turn this into an arbitrary CW proxy.
_ALLOWED_METRICS: frozenset[tuple[str, str]] = frozenset({
    ("TradingStrands", "agent.decision.latency_ms"),
    ("TradingStrands", "agent.decision.count"),
    ("TradingStrands", "agent.error.count"),
    ("TradingStrands", "agent.heartbeat.age_s"),
    ("TradingStrands", "broker.intent.received.count"),
    ("TradingStrands", "broker.intent.approved.count"),
    ("TradingStrands", "broker.intent.rejected.count"),
    ("TradingStrands", "broker.alpaca.latency_ms"),
    ("TradingStrands", "broker.alpaca.error.count"),
})

_ALLOWED_STATS: frozenset[str] = frozenset({
    "Average", "Sum", "Minimum", "Maximum", "SampleCount",
    "p50", "p90", "p95", "p99",
})


@app.post("/api/metrics/query")
async def metrics_query(
    request: Request, body: MetricsQueryRequest,
) -> dict[str, Any]:
    """Query a CloudWatch metric for a timeseries.

    Returns `{datapoints: [{ts, value}], unit}`. Empty `datapoints`
    means "no data in the window", which is distinct from an error
    (the call succeeded, nothing was emitted). The UI renders the
    two cases differently.
    """

    _ = _get_principal(request)

    key = (body.namespace, body.metric_name)
    if key not in _ALLOWED_METRICS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"metric not allowlisted: {body.namespace}/{body.metric_name}"
            ),
        )
    if body.stat not in _ALLOWED_STATS:
        raise HTTPException(
            status_code=400,
            detail=f"stat not allowed: {body.stat}",
        )
    if body.period_seconds < 60 or body.period_seconds > 86400:
        raise HTTPException(status_code=400, detail="period out of range")
    if body.lookback_seconds < 60 or body.lookback_seconds > 14 * 86400:
        raise HTTPException(status_code=400, detail="lookback out of range")

    import time as _time

    end = int(_time.time())
    start = end - body.lookback_seconds
    dims = [
        {"Name": k, "Value": v} for k, v in body.dimensions.items()
    ]
    cw = _get_cloudwatch_client()
    try:
        resp = cw.get_metric_data(
            MetricDataQueries=[{
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": body.namespace,
                        "MetricName": body.metric_name,
                        "Dimensions": dims,
                    },
                    "Period": body.period_seconds,
                    "Stat": body.stat,
                },
                "ReturnData": True,
            }],
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampAscending",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"cloudwatch unavailable: {exc}",
        ) from exc

    results = resp.get("MetricDataResults", []) or []
    if not results:
        return {"datapoints": [], "unit": ""}
    r = results[0]
    timestamps = r.get("Timestamps", []) or []
    values = r.get("Values", []) or []
    datapoints = [
        # CloudWatch returns a datetime; coerce to epoch so the UI
        # doesn't need to parse tz strings.
        {"ts": int(ts.timestamp()), "value": float(v)}
        for ts, v in zip(timestamps, values, strict=False)
    ]
    return {
        "datapoints": datapoints,
        "unit": r.get("Label", body.metric_name),
    }


@app.get("/api/supervisor/alarms")
async def supervisor_alarms(request: Request) -> dict[str, Any]:
    """Return the state of the stack's CloudWatch alarms.

    Operators use this to confirm alarm wiring at a glance: an alarm
    in ALARM state with actions_enabled=False is a silent alarm,
    which is exactly what we don't want once SNS is wired. Surfaces
    both state and actions_enabled so the UI can call that out.
    """

    _ = _get_principal(request)

    cw = _get_cloudwatch_client()
    try:
        resp = cw.describe_alarms(MaxRecords=100)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"alarm state unavailable: {exc}",
        ) from exc

    metric_alarms = resp.get("MetricAlarms", []) or []
    filtered = [
        a for a in metric_alarms
        if a.get("AlarmName") in _TRADING_STRANDS_ALARM_NAMES
    ]
    alarms = [
        {
            "name": a.get("AlarmName", ""),
            "state": a.get("StateValue", "INSUFFICIENT_DATA"),
            "reason": a.get("StateReason", ""),
            "actions_enabled": bool(a.get("ActionsEnabled", False)),
            "last_change": str(a.get("StateUpdatedTimestamp", "")),
        }
        for a in filtered
    ]
    alarms.sort(key=lambda a: a["name"])
    worst_is_alarm = any(a["state"] == "ALARM" for a in alarms)
    return {
        "ok": not worst_is_alarm,
        "alarms": alarms,
    }


@app.get("/api/supervisor/agents")
async def supervisor_agents(request: Request) -> dict[str, Any]:
    """Return the Platform Supervisor's live view of agent health.

    Reads heartbeats directly (same data the supervisor Lambda sees)
    and classifies per the supervisor's rules. Authenticated users
    only — this is infra health, not org data.

    Thresholds come from the same env vars the Lambda uses so the
    dashboard's classification matches what CloudWatch alarms on.
    """

    _ = _get_principal(request)

    from trading_strands.heartbeat.store import HeartbeatStore
    from trading_strands.platform_supervisor.supervisor import check_health

    stale = float(os.environ.get("SUPERVISOR_STALE_AFTER_SECONDS", "60"))
    missing = float(os.environ.get("SUPERVISOR_MISSING_AFTER_SECONDS", "300"))

    store = HeartbeatStore(_get_table())
    report = check_health(
        heartbeat_store=store,
        stale_after_seconds=stale,
        missing_after_seconds=missing,
    )
    return {
        "ok": report.ok,
        "total": report.total,
        "healthy": report.healthy,
        "stale": report.stale,
        "missing": report.missing,
        "agents": [
            {
                "agent_type": a.agent_type,
                "agent_id": a.agent_id,
                "last_beat_ts": a.last_beat_ts,
                "status": a.status.value,
                # Extended health-check payload per
                # docs/SPEC/observability.md §"Health checks". Present
                # for every agent; zero/empty when the agent hasn't
                # upgraded its beat() call yet (v0 bots).
                "reported_status": a.reported_status,
                "current_activity": a.current_activity,
                "last_decision_at": a.last_decision_at,
                "memory_file_cursor": a.memory_file_cursor,
                "queue_depth": a.queue_depth,
                "errors_last_hour": a.errors_last_hour,
            }
            for a in report.agents
        ],
    }


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

    Uses authoritative AWS billing data via Cost Explorer. Three grouped
    views are produced, all from the same billed numbers (not estimates):

      - by_service: Cost grouped by AWS service (DDB, Fargate, ALB, ...)
                    Authoritative; renders the daily trend chart.
      - ecs_breakdown: Within ECS, split by USAGE_TYPE so Fargate
                       vCPU-hours, memory-GB-hours, and data transfer
                       are separately visible.
      - task_allocation: A computed split of ECS compute between the
                         trading-service and dashboard-service, based
                         on each task def's declared CPU + memory
                         weight. LABELED ESTIMATE — the underlying
                         vCPU-hour cost is real, but the ratio between
                         the two services is derived from CDK-declared
                         resources because cost-allocation tag filtering
                         isn't available (linked account can't activate).

    A small "estimate" flag distinguishes computed splits from billed
    numbers so the UI never claims invented data as authoritative.
    """

    principal = _get_principal(request)
    _require(principal, Action.READ, Resource(ResourceType.COST_DATA, org_id=None))

    import datetime

    ce = boto3.client("ce")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=30)
    period = {"start": start.isoformat(), "end": end.isoformat()}

    def _groups_to_map(resp: dict[str, Any]) -> dict[str, float]:
        """Collapse a GroupBy response's last-day groups into {key: cost}."""

        out: dict[str, float] = {}
        results = resp.get("ResultsByTime", [])
        if not results:
            return out
        for group in results[-1].get("Groups", []):
            key = group["Keys"][0] if group["Keys"] else "untagged"
            out[key] = float(group["Metrics"]["UnblendedCost"]["Amount"])
        return out

    def _daily_from_service_groups(
        resp: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], float]:
        """Build the daily trend + 30-day total from a service-grouped resp."""

        out: list[dict[str, Any]] = []
        total = 0.0
        for result in resp.get("ResultsByTime", []):
            day = result["TimePeriod"]["Start"]
            components: dict[str, float] = {}
            day_total = 0.0
            for group in result.get("Groups", []):
                key = group["Keys"][0] if group["Keys"] else "other"
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount <= 0:
                    continue  # drop zero-cost noise from the UI
                components[key] = amount
                day_total += amount
            out.append({
                "date": day,
                "total": round(day_total, 4),
                "components": components,
            })
            total += day_total
        return out, total

    try:
        # Call 1: service-grouped daily trend (main chart + by_service totals)
        svc_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        daily, total = _daily_from_service_groups(svc_resp)

        # Aggregate by_service across the full 30d window.
        by_service: dict[str, float] = {}
        for day in daily:
            for svc, amt in day["components"].items():
                by_service[svc] = by_service.get(svc, 0.0) + amt
        by_service_list: list[dict[str, Any]] = [
            {"service": k, "cost": round(v, 4)} for k, v in by_service.items()
        ]
        by_service_list.sort(key=lambda x: float(x["cost"]), reverse=True)

        # Call 2: ECS breakdown by usage type over the 30d window.
        ecs_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": [
                "Amazon Elastic Container Service",
            ]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        ecs_usage = _groups_to_map(ecs_resp)
        ecs_breakdown = [
            {"usage_type": k, "cost": round(v, 4)}
            for k, v in sorted(ecs_usage.items(), key=lambda kv: -kv[1])
            if v > 0
        ]

        # Compute the task_allocation split. Hard-coded ratios from the
        # CDK stack's task defs — update these if the stack changes.
        TASK_SPECS = {
            "trading-service":   {"cpu": 512, "memory_mib": 1024},
            "dashboard-service": {"cpu": 256, "memory_mib": 512},
        }
        total_cpu = sum(s["cpu"] for s in TASK_SPECS.values())
        total_mem = sum(s["memory_mib"] for s in TASK_SPECS.values())
        # Fargate bills CPU and memory separately. Pull each line and split.
        fargate_cpu_cost = sum(
            v for k, v in ecs_usage.items() if "vCPU-Hours" in k
        )
        fargate_mem_cost = sum(
            v for k, v in ecs_usage.items() if "GB-Hours" in k
        )
        task_allocation: list[dict[str, Any]] = []
        for name, spec in TASK_SPECS.items():
            cpu_share = fargate_cpu_cost * (spec["cpu"] / total_cpu)
            mem_share = fargate_mem_cost * (spec["memory_mib"] / total_mem)
            task_allocation.append({
                "component": name,
                "cpu_cost": round(cpu_share, 4),
                "memory_cost": round(mem_share, 4),
                "total": round(cpu_share + mem_share, 4),
                "estimate": True,  # derived from CDK-declared resource specs
            })
        task_allocation.sort(key=lambda x: float(x["total"]), reverse=True)

        return {
            "period": period,
            "total_cost": round(total, 2),
            "currency": "USD",
            "daily": daily,
            "by_service": by_service_list,
            "ecs_breakdown": ecs_breakdown,
            "task_allocation": task_allocation,
            "source": "aws_cost_explorer",
        }
    except Exception as exc:
        return {
            "period": period,
            "total_cost": 0,
            "currency": "USD",
            "daily": [],
            "by_service": [],
            "ecs_breakdown": [],
            "task_allocation": [],
            "error": str(exc),
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
