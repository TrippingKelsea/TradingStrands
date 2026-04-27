# Strategy Tools & Skills

**Status:** Target (v1)
**Last updated:** 2026-04-27

This doc specifies two capabilities that can be attached to a Strategy Agent: **tools** (executable `@tool` functions that fetch data or compute things) and **skills** (reusable prompt fragments authored at the org level that get composed into the agent's system prompt). It also covers the configuration, quota, and credential model around them. If the code and this spec disagree, update one or the other.

Tools and skills are distinct on purpose:

- A **tool** is a verb. It runs code, may hit external APIs, costs tokens + external-API money, and is gated by per-strategy quota.
- A **skill** is a noun. It's text. It tells the LLM *how* to use the tools it already has, or encodes domain knowledge the org wants every strategy to share. No external calls, no quota.

---

## 1. Why tools

The v0 Strategy Agent reasons from three inputs: its strategy prompt, current prices for its symbols, and its ledger state. That's enough to implement price-structure strategies (turtle trading, mean reversion, breakout) but not enough for anything that depends on *information*: earnings proximity, news flow, corporate filings, macro releases, social-signal events.

Real trading strategies live on information asymmetry. Tools are how we inject that asymmetry into the LLM's decision loop — without turning the hot path into "call the LLM to compute things deterministic code should compute".

---

## 2. Two shapes for data delivery

Data reaches the Strategy Agent via one of two shapes:

### 2.1 Context injection (deterministic, always-on)

Small, stable-schema data that every tick wants to know about. Computed once (shared across strategies), injected into the decision prompt before the LLM is called.

Current targets:
- **Economic / earnings calendar** for the strategy's symbols (is today an earnings day? is there a Fed release scheduled in the next hour?)
- **TA snapshot** for the strategy's symbols (RSI-14, MACD, 20/50/200 MAs, Bollinger position)

Injection is cheap, deterministic, and costs nothing in tool-call accounting. The LLM can't "decide not to look"; the data is always in front of it.

### 2.2 Tool calls (LLM-driven, opt-in)

Narrative data the LLM must decide to query. Strands-native `@tool` functions. The agent chooses when the call is worth the token cost and latency.

Current targets:
- **News fetch** (NewsAPI or Alpaca news endpoint, per-org key)
- **SEC filings** (8-K, Form 4, 10-K, 10-Q; via cached EDGAR data)
- **Social sentiment** (Reddit WSB volume + X mentions for a ticker)

Invariant: **tools never mutate market or ledger state.** They read only. Writes to the ledger happen exclusively through the trade pipeline (`TradeIntent` → `TradeCoordinator` → broker) and that pipeline is not a tool.

---

## 3. Tool inventory (initial set)

Each entry declares: data shape, delivery (context vs tool), external dependency, cache, required org credential, cost envelope.

### 3.1 Economic / earnings calendar

- **Delivery**: context injection
- **Source**: Finnhub calendar API (or Alpha Vantage as fallback)
- **Cached**: `CALENDAR#{date}` row in DDB, fetched once per day by a scheduled Lambda
- **Credential**: **platform-level** `trading-strands/calendar` Secrets Manager entry (API key). Calendar data is global — AAPL's earnings date is the same regardless of which org is watching — so per-org keys would be redundant. This is a deliberate deviation from §5.1's per-org-everything default: see §5.6.
- **Cost**: flat platform-wide (one API call per day), shared across every org.
- **What the LLM sees**: a short block in the decision prompt listing today's and tomorrow's relevant events for the strategy's symbols + macro (Fed, CPI, NFP)

### 3.2 TA snapshot

- **Delivery**: context injection
- **Source**: computed from `MarketDataStore` minute bars — no external API
- **Cached**: `TA_SNAPSHOT#{symbol}#{date}#{hour}` row, computed by a scheduled Lambda (every 5 min during market hours)
- **Org credential**: none
- **Cost**: compute-only; Lambda time is the cost driver
- **What the LLM sees**: `AAPL: RSI-14=62, MACD=+0.3 rising, 20MA=$155 (above), 50MA=$150 (above), BB=upper-third`
- **Data validity**: deterministic — same bars produce same indicators. An audit pass is flagged as future work; not blocking.

### 3.3 News fetch

- **Delivery**: Strands `@tool`
- **Source**: Alpaca news endpoint by default, NewsAPI as fallback
- **Cached**: `NEWS#{symbol}#{date}` row, first-reader populates it; subsequent readers within the TTL (15 min) hit cache
- **Org credential**: `trading-strands/org/{org_id}/news` (API key; Alpaca key reuses `/org/{id}/alpaca`)
- **Cost**: per-tool-call; counts against strategy daily quota
- **Tool signature**: `fetch_news(symbol: str, hours_back: int = 24) -> list[NewsItem]`

### 3.4 SEC filings

- **Delivery**: Strands `@tool` (read-side); a scheduled EDGAR-watcher Lambda populates the cache (write-side)
- **Source**: SEC EDGAR via the daily index files + full-filing fetch for watched tickers
- **Cached**:
    - DDB index row: `FILING_INDEX#{ticker}#{accession}` with metadata (form_type, filing_date, summary)
    - S3 body: `trading-strands-filings-{account}` bucket, key `{ticker}/{form_type}/{accession}.html`
    - Lifecycle: Glacier Instant Retrieval at 30d, delete at 90d (see §5)
- **Org credential**: none (SEC EDGAR is free, but subject to strict rate limits — see §6)
- **Cost**: per-tool-call against quota, plus flat infra cost for the watcher Lambda
- **Tool signatures**:
    - `list_recent_filings(symbol: str, form_types: list[str] = ["8-K", "4"], days_back: int = 14) -> list[FilingSummary]`
    - `read_filing(accession: str) -> str`  (full body, truncated to a configurable size)

### 3.5 Social sentiment

- **Delivery**: Strands `@tool`
- **Source**: Reddit API (WSB + tailored subreddits), X API (mention counts + sampled posts)
- **Cached**: `SOCIAL#{symbol}#{date}#{hour}` row, TTL 1 hour
- **Org credential**: `trading-strands/org/{org_id}/reddit` and `.../x` (two separate secrets; an org can enable one without the other)
- **Cost**: per-tool-call against quota + metered external API costs (X especially)
- **Tool signature**: `fetch_social(symbol: str, hours_back: int = 6) -> SocialSnapshot` — returns volume counts and a sampled set of posts/titles. **Does not try to score sentiment itself**; the LLM decides what's signal given the strategy mandate.
- **Framing for the LLM**: the tool docstring explicitly notes social data is adversarial (coordinated pump groups, paid promotion, bots) so the LLM treats it with appropriate caution.

### 3.6 FDA approvals / government contracts

- **Delivery**: Strands `@tool`
- **Status**: **Deferred** — narrow value (handful of tickers), low ROI per engineering hour. Documented here so the architecture accommodates it when the need arrives.

---

## 4. Per-strategy configuration

Tools are opt-in per strategy. A strategy's `tools` field is a dict keyed on tool name; `skills` is a list of skill names the strategy pulls in:

```python
class StrategyToolConfig(BaseModel):
    enabled: bool
    daily_quota: int = 0       # hard stop at this many calls/day; 0 = disabled
    # Tool-specific config can be added here per tool type if needed.

class Strategy(BaseModel):
    # ...existing fields...
    tools: dict[str, StrategyToolConfig] = {}
    skills: list[str] = []      # names of org-authored skills to include
```

Only tools listed in the dict with `enabled=True` are wired into the bot's Strands agent. Context-injected data (calendar, TA snapshot) is also gated by entries in the same dict — an `enabled=False` or missing entry means the data is not injected. This lets a strategy opt out of TA it doesn't care about to save tokens.

Skills are looked up by name against the strategy's owning org (§8). A skill name that doesn't exist in the org is skipped with a logged warning — strategies aren't blocked from running just because a referenced skill was renamed or deleted.

---

## 5. Per-org credentials + filings cache

### 5.1 Credentials

External APIs with per-account keys are configured at the org level and stored in Secrets Manager under a deterministic path:

```
trading-strands/org/{org_id}/news         # NewsAPI or equivalent
trading-strands/org/{org_id}/calendar     # Finnhub or Alpha Vantage
trading-strands/org/{org_id}/reddit       # Reddit client_id/secret + user_agent
trading-strands/org/{org_id}/x            # X bearer token
```

Same pattern as the existing `trading-strands/org/{org_id}/alpaca` secret. Read access scoped to the trading task role (and the scheduled-fetcher Lambdas, for tools that populate shared caches).

### 5.2 Missing-credential behavior

A strategy that enables a tool whose required credential is missing **fails at strategy start**, not mid-tick. The StrategySupervisor's task definition includes a startup check that reads the configured tools and asserts each required credential is resolvable. Matches the fail-closed posture used for Alpaca creds today.

### 5.3 Dashboard ungating

The strategy edit form lists every available tool. A tool's row is **unselectable** (greyed-out checkbox) when either:

- the org hasn't provisioned the required credential, or
- the orgadmin has disabled the tool at the org level.

Orgadmins provision credentials and manage per-org tool availability in a separate Tools section of the org admin view; once both are satisfied, the checkbox on strategy forms becomes selectable.

### 5.4 Org gate vs. strategy selection (important)

The org-level control is a **gate**, not a force-on:

- **Org disables a tool** → strategies cannot enable it. Existing strategies that had it enabled keep the config bit but the bot is wired without the tool until the org re-enables.
- **Org enables a tool** → strategies *may* enable it. Enabling at the org level never turns a tool on for any strategy that hasn't explicitly opted in.

Rationale: a strategy's prompt is its contract. The orgadmin can't silently change what tools a strategy has access to without the strategy's author acknowledging it. The gate direction is one-way: the org can take a tool away from a strategy, never add one.

### 5.5 Platform-level credentials (exception to per-org default)

Most external credentials are per-org (§5.1). Two kinds are
platform-level instead:

- **Calendar data** (§3.1) — global by construction; running one
  fetch per org would multiply cost with no data difference. Key
  at `trading-strands/calendar`.
- **SEC EDGAR** (§3.4) — no key required; SEC expects only a
  User-Agent header identifying the fetcher. The watcher Lambda
  sends a TradingStrands-identifying UA.

When adding a new tool, the default remains per-org. Only move
something to platform-level when the data is structurally global
AND the economic cost of per-org fetches is non-trivial. Document
the reasoning in this section when the exception is made.

### 5.6 SEC filings S3 cache

Separate from the agent-memory bucket:

- **Bucket**: `trading-strands-filings-{account}`
- **Layout**: `{ticker}/{form_type}/{accession}.html` (accession number is SEC's canonical identifier — natural dedup key)
- **Lifecycle**: transition to Glacier Instant Retrieval at 30 days (cost savings while staying cheaply readable for audits), delete at 90 days
- **Cross-org-shared**: filings are public data; caching them per-org would be waste. Read access granted to any strategy task.

---

## 6. Rate-limit discipline

Several external sources have strict rate limits:

- **SEC EDGAR**: 10 requests/second per IP, User-Agent header required. A cold cache + strategy-triggered fetch path would hit the limit the moment two strategies care about the same ticker. Solution: **pull-on-cache-miss is not allowed**. A scheduled `EdgarWatcher` Lambda fetches new filings for watched tickers at a controlled cadence (every 15 min during market hours) and populates the cache. Strategy tools read from cache only.
- **X**: rate limits depend on tier (Free → Basic → Pro). Enforce the org's plan tier as part of the secret configuration and reject calls that would breach. Document the limit in the tool docstring so the LLM understands latency behavior.
- **Reddit**: 100 requests/minute per OAuth app. Share one app across the org's strategies with the user_agent identifying TradingStrands + org name.
- **Finnhub / Alpha Vantage / NewsAPI**: quota-based (calls/minute or calls/day). Same cache-first pattern: the calendar fetcher Lambda is the only writer; strategies read from DDB.

---

## 7. Quota enforcement

Every tool call counts against the calling strategy's per-day budget (`StrategyToolConfig.daily_quota`). Enforcement is **hard stop** — when the day's budget is exhausted, subsequent calls raise `QuotaExceeded` synchronously. The LLM receives the error, reasons about it, and typically holds for the day. We deliberately prefer this over soft-degrade to stale cache, because a strategy that can decide to trade on stale data is worse than one that knows it cannot fetch fresh data.

### 7.1 Storage

Per-strategy per-day counters live in DDB under:

```
TOOL_QUOTA#{strategy_id}#{date}    → {tool_name: count, ...}
```

Atomic `ADD` on the nested counter; TTL at end of day + 30 (so an operator can inspect yesterday's usage even after it's "expired"). Same pattern as `TOKEN#{org_id}#{date}`.

### 7.2 Accounting correctness

The counter is incremented **before** the tool call runs, not after. A tool that raises mid-call still costs quota — matches the intent that the call "happened" from the external API's perspective (even on a 500 we may have hit the endpoint).

### 7.3 Shared-cache reads don't count

When a tool call is served entirely from DDB/S3 cache (no external API call), it does **not** count against quota. The quota is a cost-control mechanism, and a cached read costs ~nothing. Exposed as `call_type="cache_hit"` in the accounting row so operators can see cache effectiveness.

### 7.4 What "day" means

UTC-midnight rollover for the counter key. Matches the rest of the codebase's UTC-at-boundary convention.

---

## 8. Skills

A **skill** is a named markdown file authored at the org level, selectable per-strategy, that gets composed into the Strategy Agent's system prompt at bot-start. It's text — no code, no external calls, no quota.

Uses:

- Encode domain knowledge the org wants every strategy to share ("Options Greeks cheat sheet", "rules for trading around FOMC meetings")
- Codify behavioral patterns without rewriting them in each strategy prompt ("Morning prep checklist: check calendar, review overnight news, assess vol regime")
- Distribute updates to shared reasoning patterns — edit one skill, every strategy that uses it picks up the change on next bot restart

### 8.1 Authorship + scope

- **Authored by**: orgadmins in the owning org. Same write gate as Alpaca credential configuration.
- **Scoped to**: the owning org only. A skill belongs to one org; strategies in that org can reference it by name. No cross-org sharing, no system-wide skills in v1.
- **No versioning in v1.** Editing a skill takes effect on the next bot restart — treat them like shared source files. Authors communicate edits out-of-band. If this becomes a problem we revisit.

### 8.2 Storage

Skills live in DDB alongside strategies:

```
pk = SKILL#{org_id}#{skill_name}
    skill_name   — unique within the org, slug-cased
    markdown     — the skill body
    author_user_id
    created_at
    updated_at
```

One row per skill. Skill body is bounded to 32 KB — if a skill is longer than that, it should probably be two skills.

Strategies reference skills by name. On bot-start the skill bodies are resolved against the strategy's `org_id` and composed into the system prompt (§8.4). Missing skills log a warning and are skipped; the bot starts anyway.

### 8.3 Tool / skill distinction

If you're deciding where a new capability belongs:

| Question | Tool | Skill |
|---|---|---|
| Does it run code? | yes | no |
| Does it hit an external API? | possibly | no |
| Does it cost money per call? | possibly | no, tokens only |
| Is it deterministic text? | no | yes |
| Does it need per-org credentials? | possibly | no |

Grey area — "compute portfolio VaR":
- If it's deterministic Python that computes VaR from the ledger → **tool** (uses the same quota accounting; may be cache-hit if we memoize)
- If it's a prompt telling the LLM how to reason about VaR ("check each position's 1-day 99% VaR using X formula, flag if aggregate > 2% of equity") → **skill**

Both can coexist for the same concept. A skill can instruct the LLM to call a particular tool in a particular way; the tool does the math, the skill tells the LLM when to call it and how to interpret the result.

### 8.4 Composition into the system prompt

When a strategy has skills attached, they're composed into the bot's system prompt in labelled sections, preserving authorship boundaries:

```markdown
<base system prompt — the disciplined-trading-bot framing>

# Skill: morning_prep
<morning_prep.md contents>

# Skill: options_greeks
<options_greeks.md contents>

# Strategy: <strategy name>
<strategy.md markdown>
```

Skill section order matches the order of names in `Strategy.skills`. Authors of complex strategies can use that ordering to build progressive context (general knowledge first, strategy-specific last).

### 8.5 Invariants

- Skills are read-only at runtime. Strategy bots never modify them.
- Skills are per-org. A strategy's `org_id` determines which skill namespace it reads from.
- No sysadmin-authored system skills in v1. If we ship default skills later, they'll be either copied per-org on bootstrap (explicit) or introduced as a separate "system skill" resource type (not implicit).
- Skills contain no secrets. Since they're rendered into every bot's system prompt they're visible in logs, reflection memory, etc. — they are public knowledge within the org by construction.

### 8.6 Dashboard UX (skills)

- **Admin → Skills** (new section, orgadmin-only): list, create, edit, delete. Editor is a plain markdown textarea matching the strategy editor's look.
- **Strategy edit form → Skills panel**: multi-select listing the org's skills. Each entry shows name + a truncated body preview. Order of selection preserved (drives §8.4 order).
- **Strategy view**: the composed system prompt is previewable at read time so authors can see what the LLM will actually see. Helps catch "I forgot I had morning_prep attached" surprises.

---

## 9. Tool architecture

Strands-native `@tool` functions for v1. Rationale:

- Simplest integration with the existing Strategy Agent
- No extra Lambda infrastructure to maintain
- Tool calls run in-process alongside the bot's LLM invocation; latency is just the external-API round-trip

Tools live under `src/trading_strands/tools/<tool_name>.py`. Each module exports a factory `make_<tool>_tool(context) -> StrandsTool` that takes a context object (org creds, quota accountant, cache handles) and returns the bound tool.

When a specific tool grows a scaling or cost envelope that warrants its own infrastructure (e.g., a compute-heavy TA variant, or a streaming social watcher), it can be promoted to a Lambda with the same signature. The interface stays stable; only the implementation moves.

---

## 10. Tool call observability

Every tool call emits an EMF metric:

- **Metric**: `tool.call.count`, `tool.call.latency_ms`
- **Dimensions**: `tool` (tool name), `outcome` (success / quota_exceeded / error / cache_hit)
- **Extras (searchable in Logs Insights)**: `strategy_id`, `org_id`, `symbol` when relevant

The existing `token_telemetry` module already records LLM token usage per strategy; tool-call accounting parallels it and shares dashboards.

### 10.1 Alarm

A single CloudWatch alarm on `tool.call.count` with `outcome=quota_exceeded` — threshold and period TBD once we have usage baselines. Fires disabled like the halt alarms; SNS wiring deferred.

---

## 11. Dashboard UX

### 11.1 Strategy edit form

A **Tools** panel under the markdown editor shows a row per available tool. Every tool in the inventory is listed, regardless of whether it's currently selectable — the author always sees the full menu and what's blocking each unavailable option:

| Tool | Enabled | Daily quota | State |
|---|---|---|---|
| Earnings calendar | ☑ | — (context) | Auto-injected into decisions |
| TA snapshot | ☑ | — (context) | Auto-injected |
| News | ☐ (disabled) | — | *Org hasn't set NewsAPI key* |
| SEC filings | ☑ | 50 | Cached; hits cache often |
| Social sentiment | ☐ (disabled) | — | *Org disabled this tool* |

Rows whose checkbox is **unselectable** (disabled) carry an inline reason: missing credential or org-level disable. Clicking the disabled reason text links to the Admin → Tools page. Existing config bits on a strategy persist across org-disable — re-enabling at the org level restores the strategy's prior tool state.

Below the Tools panel, a **Skills** multi-select lists the org's authored skills. Order of selection is preserved and drives the composition order in the system prompt (§8.4).

### 11.2 Admin → Tools

New section under the existing Admin tab, orgadmin-only:

- Per-tool credential entry (API keys, OAuth secrets) writing to the per-org Secrets Manager paths
- Per-tool enable/disable at the org level. The control is a **gate**: disabling hides the tool from every strategy edit form in the org and takes it away from running bots on next restart. It does not force a tool on — strategies must still opt in individually (§5.4).
- Current-day quota usage across strategies, for cost visibility

### 11.3 Admin → Skills

New section under the Admin tab, orgadmin-only:

- List, create, edit, delete per-org skills. Editor is a plain markdown textarea.
- Skills are shown with their name, author, last-updated timestamp, and a truncated preview.
- Deleting a skill that a strategy references is allowed but flagged with a "used by N strategies" warning; affected strategies log a missing-skill warning on next restart and run without it.

### 11.4 Monitor tab

Tool-call rate and error counts surface as a small panel on the existing Monitor tab, drawing from the same EMF metrics dashboards read.

---

## 12. Invariants (summary)

1. **Tools read only.** Trade intents flow through the trade pipeline; tools never submit orders.
2. **Keys are per-org by default.** External API credentials live at the org level unless the data is structurally global AND per-org fetches would duplicate cost (the platform-level exception, §5.5). Per-org keys fail closed at strategy start when missing.
3. **Quota is hard-stop.** `QuotaExceeded` is preferred over stale-data soft-degrade.
4. **Cache before external call.** For rate-limited sources, the cache is authoritative; scheduled fetchers are the only writers.
5. **Adversarial-data tools carry explicit framing.** The social-sentiment tool's docstring names the adversarial surface so the LLM's reasoning takes it into account.
6. **Context-injected data is always-on per-strategy-opt-in.** It's injected into the decision prompt, not fetched by the LLM. Strategies opt out to save tokens; they cannot "forget" to look.
7. **Org control is a gate, not a force.** An orgadmin can take a tool away from strategies (disable at org level) but never add a tool to a strategy that didn't opt in.
8. **Skills are read-only at runtime + per-org.** Strategy bots never mutate skills. Skills belong to exactly one org; no cross-org sharing in v1.
9. **Skills contain no secrets.** They render into every bot's system prompt and therefore into reflection memory, logs, and self-critique output — treat them as public within the org.

---

## 13. Open questions

- **TA snapshot cadence and indicator set** — 5-minute refresh is a guess; real cadence should match the shortest strategy tick interval we support. The default indicator set (RSI, MACD, MAs, Bollinger) is a starting point; additions (VWAP, ATR, stochastics) are one-liners once the framework exists.
- **Social sentiment scoring vs. raw data** — v1 returns raw counts and sampled posts. If that proves inadequate, a later revision could add a scoring step, but the scorer has to be adversarial-aware.
- **Calendar provider** — Finnhub vs Alpha Vantage vs Benzinga; pick one, fall back to another. Starting assumption: Finnhub.
- **Tool marketplace** — longer term, should orgs be able to define their own tools (declarative schema, containerized implementation)? Not in v1.
