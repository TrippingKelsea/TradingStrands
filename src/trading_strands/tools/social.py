"""Social sentiment tool — Reddit only for v1.

Per SPEC §3.5 and §11.5: returns RAW counts and sampled posts. Does
NOT score sentiment — the LLM interprets, with explicit adversarial
framing in the docstring so the LLM knows the data includes bots,
coordinated pumps, and paid promotion.

X (formerly Twitter) is a separate tool factory — same shape,
different credentials — added in a follow-on commit. An org can
enable one without the other per SPEC §3.5.
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

# 1-hour freshness per §3.5. Social data moves fast but a strategy
# that ticks every 5s shouldn't hammer Reddit for each tick.
_CACHE_TTL_SECONDS = 3600
_ROW_TTL_SECONDS = 24 * 3600
_SOCIAL_CACHE_PK_PREFIX = "SOCIAL#"
# Cap sampled posts. A few representative posts is what the LLM
# can reason about — handing it 500 titles would just eat tokens.
_SAMPLE_POST_LIMIT = 10

# Subreddits searched. WSB is the obvious one; the others catch
# the ticker-specific investing communities.
_DEFAULT_SUBREDDITS = (
    "wallstreetbets",
    "stocks",
    "investing",
    "StockMarket",
)

# Explicit framing for the LLM. Also pinned in a test as a regression
# guard — if this marker disappears, SPEC invariant #5 (adversarial
# framing) has regressed.
REDDIT_ADVERSARIAL_DOCSTRING_MARKER = (
    "Social data is adversarial. Coordinated pump groups, paid "
    "promotion, and bots routinely manipulate mention volume and "
    "sentiment. Treat counts as weak signal, not fact. Cross-"
    "reference with filings, price action, and news before acting."
)


class SocialSnapshot(BaseModel):
    """One cached social snapshot for a symbol.

    Deliberately no `sentiment` / `score` aggregate field — see
    §3.5 framing. Individual post dicts may have a 'score' from
    Reddit's upvotes; the top-level object does not.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    cached_at: int
    mention_count: int
    sample_posts: list[dict[str, Any]]


class SocialCache:
    def __init__(self, table: Any) -> None:
        self._table = table

    @staticmethod
    def _pk(symbol: str) -> str:
        return f"{_SOCIAL_CACHE_PK_PREFIX}{symbol.upper()}#cache"

    def get(self, symbol: str) -> SocialSnapshot | None:
        resp = self._table.get_item(Key={"pk": self._pk(symbol)})
        item = resp.get("Item")
        if item is None:
            return None
        cached_at = int(item.get("cached_at", 0))
        if (time.time() - cached_at) > _CACHE_TTL_SECONDS:
            return None
        raw = json.loads(str(item.get("payload_json", "{}")))
        return SocialSnapshot.model_validate(raw)

    def put(self, symbol: str, snap: SocialSnapshot) -> None:
        now = int(time.time())
        self._table.put_item(Item={
            "pk": self._pk(symbol),
            "symbol": symbol.upper(),
            "cached_at": now,
            "payload_json": snap.model_dump_json(),
            "ttl": now + _ROW_TTL_SECONDS,
        })


class RedditClient:
    """Minimal Reddit search client using the app-only OAuth token.

    Reddit requires:
      - client_id + client_secret (app credentials)
      - unique User-Agent identifying the requester
    All three come from the org's Secrets Manager entry at
    trading-strands/org/{org_id}/reddit.
    """

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 — URL, not a secret
    SEARCH_URL_TMPL = (
        "https://oauth.reddit.com/r/{sub}/search"
        "?q={q}&restrict_sr=on&sort=new&t=day&limit=25"
    )

    def __init__(
        self, client_id: str, client_secret: str, user_agent: str,
    ) -> None:
        if not client_id or not client_secret or not user_agent:
            msg = (
                "RedditClient requires client_id, client_secret, and "
                "user_agent (reddit's rules)"
            )
            raise ValueError(msg)
        self._cid = client_id
        self._csec = client_secret
        self._ua = user_agent
        self._token: str | None = None
        self._token_exp: int = 0

    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        import base64

        creds = base64.b64encode(
            f"{self._cid}:{self._csec}".encode(),
        ).decode()
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
        }).encode()
        req = urllib.request.Request(  # noqa: S310 — https const URL
            self.TOKEN_URL, data=data,
            headers={
                "Authorization": f"Basic {creds}",
                "User-Agent": self._ua,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        token = str(body.get("access_token", ""))
        expires_in = int(body.get("expires_in", 3600))
        if not token:
            msg = "reddit auth returned no access_token"
            raise RuntimeError(msg)
        self._token = token
        self._token_exp = int(time.time()) + expires_in
        return token

    def search(
        self, symbol: str, hours_back: int,
    ) -> list[dict[str, Any]]:
        """Search the default subreddits for the symbol. Returns a
        list of post dicts with title/score/author/created_utc/url.
        Time-window filtering is done client-side (Reddit's 't'
        param is coarse)."""

        token = self._auth()
        cutoff = time.time() - hours_back * 3600
        out: list[dict[str, Any]] = []
        for sub in _DEFAULT_SUBREDDITS:
            url = self.SEARCH_URL_TMPL.format(
                sub=sub,
                q=urllib.parse.quote(f"${symbol} OR {symbol}"),
            )
            req = urllib.request.Request(  # noqa: S310
                url, headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self._ua,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                logger.exception(
                    "social.reddit.search_failed", sub=sub, symbol=symbol,
                )
                continue
            for child in payload.get("data", {}).get("children", []):
                data = child.get("data", {})
                created = float(data.get("created_utc", 0))
                if created < cutoff:
                    continue
                out.append({
                    "subreddit": sub,
                    "title": str(data.get("title", "")),
                    "score": int(data.get("score", 0)),
                    "author": str(data.get("author", "")),
                    "created_utc": int(created),
                    "url": str(data.get("url", "")),
                })
        # Newest first, then by score.
        out.sort(key=lambda p: (p["created_utc"], p["score"]), reverse=True)
        return out


def _run_social_fetch(
    symbol: str,
    hours_back: int,
    ctx: ToolContext,
    cache: SocialCache,
    client: Any,
    daily_quota: int,
) -> SocialSnapshot:
    """Orchestration: cache-first, quota-accounted, EMF emission."""

    sym = symbol.upper()

    cached = cache.get(sym)
    if cached is not None:
        ctx.quota_store.record_cache_hit(ctx.strategy_id, "social")
        emit_tool_outcome(
            "social", "cache_hit",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        return cached

    try:
        ctx.quota_store.reserve(ctx.strategy_id, "social", limit=daily_quota)
    except QuotaExceeded:
        emit_tool_outcome(
            "social", "quota_exceeded",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        raise

    try:
        with tool_call_timer(
            "social",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        ):
            posts = client.search(symbol=sym, hours_back=hours_back)
    except Exception:
        emit_tool_outcome(
            "social", "error",
            strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
        )
        raise

    snap = SocialSnapshot(
        symbol=sym,
        cached_at=int(time.time()),
        mention_count=len(posts),
        sample_posts=posts[:_SAMPLE_POST_LIMIT],
    )
    cache.put(sym, snap)
    emit_tool_outcome(
        "social", "success",
        strategy_id=ctx.strategy_id, org_id=ctx.org_id, symbol=sym,
    )
    return snap


def _load_reddit_creds(
    secrets_client: Any, org_id: str,
) -> tuple[str, str, str]:
    """Read Reddit creds from trading-strands/org/{org_id}/reddit.

    Reusing secret_name_for() + a per-service suffix is tempting
    but the existing helper is Alpaca-specific. Hand-assemble the
    name here to stay explicit about where this secret lives.
    """

    # secret_name_for() today gives trading-strands/org/{id}/alpaca;
    # swap the trailing segment.
    base = secret_name_for(org_id).rsplit("/", 1)[0]
    secret_name = f"{base}/reddit"
    resp = secrets_client.get_secret_value(SecretId=secret_name)
    payload = json.loads(resp.get("SecretString", "{}"))
    cid = payload.get("REDDIT_CLIENT_ID", "")
    csec = payload.get("REDDIT_CLIENT_SECRET", "")
    ua = payload.get(
        "REDDIT_USER_AGENT",
        f"TradingStrands/1.0 (by /u/trading-strands; org={org_id})",
    )
    if not cid or not csec:
        msg = (
            f"org {org_id} reddit creds missing REDDIT_CLIENT_ID "
            "or REDDIT_CLIENT_SECRET — social tool cannot run"
        )
        raise RuntimeError(msg)
    return cid, csec, ua


def make_social_tool(
    ctx: ToolContext, daily_quota: int = 50,
) -> Any:
    """Factory returning the Strands @tool callable."""

    from strands import tool

    cache = SocialCache(ctx.table)
    _client_cache: dict[str, RedditClient] = {}

    def _client() -> RedditClient:
        if "_c" not in _client_cache:
            cid, csec, ua = _load_reddit_creds(
                ctx.secrets_client, ctx.org_id,
            )
            _client_cache["_c"] = RedditClient(cid, csec, ua)
        return _client_cache["_c"]

    @tool
    def fetch_social(
        symbol: str, hours_back: int = 6,
    ) -> dict[str, Any]:
        snap = _run_social_fetch(
            symbol=symbol, hours_back=hours_back,
            ctx=ctx, cache=cache, client=_client(),
            daily_quota=daily_quota,
        )
        return snap.model_dump(mode="json")

    # Assemble the docstring explicitly rather than using .format —
    # the return-shape sentence names fields like {symbol} literally
    # and .format() would interpret those as placeholders. Direct
    # assignment keeps the marker intact for the test invariant.
    fetch_social.__doc__ = (
        "Fetch recent Reddit mentions of a stock symbol.\n\n"
        "Returns a dict with symbol, mention_count, and sample_posts "
        "(each with title, score, author, subreddit, created_utc, "
        "url). Queries WSB, r/stocks, r/investing, r/StockMarket.\n\n"
        "Does NOT score sentiment. Interpretation is your job.\n\n"
        "ADVERSARIAL DATA WARNING:\n"
        + REDDIT_ADVERSARIAL_DOCSTRING_MARKER + "\n\n"
        "Cache TTL is about an hour; repeat calls within that window "
        "are free. Quota exhaustion means the tool will refuse to "
        "return fresh data until tomorrow — plan your reads."
    )

    return fetch_social
