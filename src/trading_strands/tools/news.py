"""News fetch tool.

First actual @tool call in the framework (commit 4 of the tools+skills
build). Cache-first: the hot path hits DDB, not the news API. Quota
accounting through ToolQuotaStore — external calls consume, cache
hits don't.

Data source: Alpaca's news endpoint, reusing the org's existing
Alpaca credentials. Keeping to the broker's own creds avoids yet
another per-org secret for an already-provisioned relationship.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from trading_strands.alpaca_secrets.store import secret_name_for
from trading_strands.tool_quota.store import QuotaExceeded
from trading_strands.tools.base import ToolContext
from trading_strands.tools.observability import (
    emit_tool_outcome,
    tool_call_timer,
)

logger = structlog.get_logger()

# Cache entry is considered stale after this long. News moves fast
# enough that hour-granularity is right — an earnings release is
# stale 15 min later, but the cache also shields the tool from being
# hammered by a bot that ticks every 5s.
_CACHE_TTL_SECONDS = 3600
# DDB row retention beyond the freshness window. Keeps a day of
# history for debugging; not meant to satisfy operational reads.
_ROW_TTL_SECONDS = 24 * 3600

_NEWS_CACHE_PK_PREFIX = "NEWS#"
# One row per symbol covers the most-recent-hour use case; an
# incoming fetch with different `hours_back` still keys on symbol
# and refreshes when stale. Keeps cache size bounded.


class NewsItem(BaseModel):
    """One news headline. Minimal fields — enough for the LLM to
    reason with a sample of stories, not a full news feed."""

    model_config = ConfigDict(extra="ignore")

    id: str
    headline: str
    summary: str
    url: str
    source: str
    created_at: int | str   # epoch or RFC3339; we store what arrives
    symbols: list[str]


class NewsCache:
    """DDB-backed per-symbol news cache (hourly freshness)."""

    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _pk(symbol: str) -> str:
        return f"{_NEWS_CACHE_PK_PREFIX}{symbol.upper()}#cache"

    def get(self, symbol: str) -> list[NewsItem] | None:
        """Return cached items if fresh (within TTL), else None."""

        resp = self._table.get_item(Key={"pk": self._pk(symbol)})
        item = resp.get("Item")
        if item is None:
            return None
        cached_at = int(item.get("cached_at", 0))
        if (time.time() - cached_at) > _CACHE_TTL_SECONDS:
            return None
        raw = json.loads(str(item.get("payload_json", "[]")))
        return [NewsItem.model_validate(x) for x in raw]

    def put(self, symbol: str, items: list[NewsItem]) -> None:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": self._pk(symbol),
            "symbol": symbol.upper(),
            "cached_at": now,
            "payload_json": json.dumps(
                [x.model_dump(mode="json") for x in items],
            ),
            "ttl": now + _ROW_TTL_SECONDS,
        })


class AlpacaNewsClient:
    """Minimal Alpaca news HTTP client.

    Stdlib only — one GET with two headers. Endpoint:
        GET https://data.alpaca.markets/v1beta1/news?symbols=...&start=...&limit=...
    """

    BASE = "https://data.alpaca.markets"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key

    def fetch(
        self,
        symbols: tuple[str, ...],
        start_ts: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params = {
            "symbols": ",".join(symbols),
            "start": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ts),
            ),
            "limit": str(limit),
        }
        url = f"{self.BASE}/v1beta1/news?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "APCA-API-KEY-ID": self._api_key,
                "APCA-API-SECRET-KEY": self._secret_key,
                "User-Agent": "TradingStrands news-tool",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = resp.read()
        payload = json.loads(body.decode("utf-8"))
        return list(payload.get("news", []))


def _to_news_item(raw: dict[str, Any]) -> NewsItem:
    """Convert Alpaca's JSON shape to our NewsItem."""

    return NewsItem(
        id=str(raw.get("id", "")),
        headline=str(raw.get("headline", "")),
        summary=str(raw.get("summary", "")),
        url=str(raw.get("url", "")),
        source=str(raw.get("source", "alpaca")),
        created_at=raw.get("created_at", int(time.time())),
        symbols=list(raw.get("symbols", []) or []),
    )


def _run_news_fetch(
    symbol: str,
    hours_back: int,
    ctx: ToolContext,
    cache: NewsCache,
    client: Any,
    daily_quota: int,
) -> list[NewsItem]:
    """Shared implementation between the live tool and tests.

    Cache-first. Quota reserved BEFORE the external call so failed
    calls still cost — matches the "hard stop + incremented ahead"
    policy from SPEC/tools.md §7.2.

    Emits one `tool.call.count` metric per invocation (outcome =
    cache_hit | quota_exceeded | success | error) and a
    `tool.call.latency_ms` for the external-call path. See
    docs/SPEC/tools.md §10 for the observability contract.
    """

    sym = symbol.upper()

    cached = cache.get(sym)
    if cached is not None:
        ctx.quota_store.record_cache_hit(ctx.strategy_id, "news")
        emit_tool_outcome(
            "news", "cache_hit",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        return cached

    # Reserve quota first. Raises QuotaExceeded if over limit — emit
    # the outcome metric before re-raising so the alarm path sees it.
    try:
        ctx.quota_store.reserve(ctx.strategy_id, "news", limit=daily_quota)
    except QuotaExceeded:
        emit_tool_outcome(
            "news", "quota_exceeded",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        raise

    # External fetch. Errors propagate — the reserve already landed.
    start_ts = int(time.time()) - hours_back * 3600
    try:
        with tool_call_timer(
            "news",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        ):
            raw = client.fetch(symbols=(sym,), start_ts=start_ts)
    except Exception:
        emit_tool_outcome(
            "news", "error",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        raise

    items = [_to_news_item(r) for r in raw]

    # Cache even empty results — a quiet symbol shouldn't hammer
    # the API every tick just because there's nothing to show.
    cache.put(sym, items)
    emit_tool_outcome(
        "news", "success",
        strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
    )
    return items


def _load_alpaca_creds_for_org(
    secrets_client: Any, org_id: str,
) -> tuple[str, str]:
    """Read the org's Alpaca creds from Secrets Manager. Raises if
    missing — caller should ensure the creds were provisioned at
    strategy-start time."""

    secret_name = secret_name_for(org_id)
    resp = secrets_client.get_secret_value(SecretId=secret_name)
    payload = json.loads(resp.get("SecretString", "{}"))
    api_key = payload.get("ALPACA_API_KEY", "")
    secret_key = payload.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        msg = (
            f"org {org_id} Alpaca creds missing ALPACA_API_KEY or "
            "ALPACA_SECRET_KEY — news tool cannot run"
        )
        raise RuntimeError(msg)
    return api_key, secret_key


def make_news_tool(ctx: ToolContext, daily_quota: int = 50) -> Any:
    """Factory that returns a @tool-decorated callable the Strands
    agent can invoke.

    Imported strands at factory time so the rest of this module
    stays unit-testable without Bedrock installed.
    """

    from strands import tool

    cache = NewsCache(ctx.table)
    # Lazy client — creds read on first call, cached for the agent's
    # lifetime. If a creds rotation lands, the agent restart picks
    # up the new ones.
    _client_cache: dict[str, AlpacaNewsClient] = {}

    def _client() -> AlpacaNewsClient:
        if "_c" not in _client_cache:
            api, sec = _load_alpaca_creds_for_org(
                ctx.secrets_client, ctx.org_id,
            )
            _client_cache["_c"] = AlpacaNewsClient(api, sec)
        return _client_cache["_c"]

    @tool
    def fetch_news(symbol: str, hours_back: int = 24) -> list[dict[str, Any]]:
        """Fetch recent news headlines for a symbol.

        Returns a list of {id, headline, summary, url, source,
        created_at, symbols} dicts covering the last `hours_back`
        hours. Results are cached per symbol for ~1 hour; cache hits
        don't count against your daily quota.

        If your daily news quota is exhausted, this will raise —
        the LLM receives the error and typically responds by
        holding for the day.
        """

        items = _run_news_fetch(
            symbol=symbol, hours_back=hours_back,
            ctx=ctx, cache=cache, client=_client(),
            daily_quota=daily_quota,
        )
        return [it.model_dump(mode="json") for it in items]

    return fetch_news
