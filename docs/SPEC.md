# TradingStrands — Design Specification

**Status:** Draft v0.2
**Last updated:** 2026-04-13

This is the authoritative design document for TradingStrands. If the code and the spec disagree, the spec is either wrong or out of date — in which case update it.

---

## 1. Vision

TradingStrands is a **strategy-as-prompt** trading agent framework. A user writes a trading strategy in natural language — ranging from trivial ("use the turtle trading methodology with $1000") to sophisticated ("long/short volatility arbitrage on SPX options using the following model…") — and the system runs it as a live agent against real markets, under the supervision of a deterministic risk manager.

Long-term, this may evolve into a consumer-facing product with a webchat interface. For now, it is a CLI-first framework targeting a single expert user.

---

## 2. Core philosophy

> **There is no environment like production to test what happens in production.**

The markets are a chaotic environment that cannot be faithfully simulated. Backtests and paper-trading produce false confidence by hiding latency, slippage, partial fills, order book dynamics, and the emotional/structural feedback loops that define real execution. TradingStrands therefore does not ship a backtesting or paper-trading mode — by design, not by omission.

This philosophy is non-negotiable and shapes every design choice below.

### 2.1 The compensating mechanism: the what-if ledger

The absence of simulation is compensated by a **counterfactual tracker**. Every decision point records:

- The action the agent took (if any)
- The actions the agent *considered and passed on*

Passed-on trades are virtually filled at the decision moment's market price and marked to market continuously. This lets the operator reason about "what if I had entered that trade?" using *real market data* rather than a simulated environment. Exit logic for what-if trades is **deferred** to a later scoping pass.

---

## 3. Capital model

TradingStrands runs multiple strategy bots concurrently against a **shared real brokerage account**. Each bot is assigned a virtual starting capital and tracks its own independent balance sheet — there is no per-bot sub-account at the broker level.

### 3.1 Per-bot ledger

Each strategy bot owns:

- `starting_capital` (fixed at bot launch)
- `realized_pnl` (closed positions, fully burdened — inclusive of all fees)
- `unrealized_pnl` (open positions, marked to market with entry fees already deducted)
- `equity` = `starting_capital + realized_pnl + unrealized_pnl`
- `high_water_mark`
- `open_positions[]` — each with fully-burdened cost basis (execution price + fees)
- `order_history[]`
- `fee_ledger[]` — granular fee breakdown per fill (see §3.4)

### 3.4 Fee tracking

Every fill records a granular fee breakdown as separate line items:

- **Commission** (if applicable)
- **Regulatory fees** — SEC fee, TAF (Trading Activity Fee), FINRA fee
- **Options fees** — per-contract, exercise/assignment fees
- **Crypto costs** — spread cost, network fees
- **Other** — extensible for broker-specific charges

Fees are accounted for in two ways simultaneously:

1. **Fully burdened cost basis** — positions carry fees in their cost basis so that PnL reflects true win/loss viability. A trade is only profitable if it clears its fees.
2. **Separate line items** — the `fee_ledger` records each fee component independently, enabling auditing, reconciliation, and fee analysis across strategies.

The broker adapter maintains and exposes its fee schedule to strategy bots so they can factor expected fees into position sizing and entry/exit decisions.

### 3.2 Risk budget scaling

Risk limits scale with **current equity**, not starting capital. Example: a bot given $1,000 on Monday that reaches $2,000 by Wednesday now operates with risk limits sized against $2,000. Rules:

- Per-position risk: percentage of current equity.
- Daily loss cap: percentage of current equity at session start.
- Drawdown-from-high-water-mark cap: percentage of `high_water_mark`.

### 3.3 Allocation reconciliation

Because multiple bots share a broker account, the ledger must reconcile virtual positions against real broker state on every orchestrator tick. Divergence (e.g., a fill the ledger did not expect) triggers a circuit breaker — see §7.

---

## 4. Strategy-as-prompt

A strategy is a markdown document written by the user. The document is ingested by a **compilation agent** (Strands) which extracts two artifacts:

### 4.1 The TTA spec (compiled, deterministic)

The **threshold-to-action (TTA)** spec is a structured predicate definition. It declares the conditions under which the strategy bot should be woken for a decision. TTA predicates are evaluated by the orchestrator tick in code — **never via an LLM call in the hot path**.

TTA predicates can reference:

- Price levels (absolute, relative, moving-average based)
- Indicator values (RSI, MACD, Bollinger, etc.)
- Greeks (delta, gamma, vega, theta)
- Volatility measures (realized, implied, term structure)
- Volume / flow signals
- Time-based triggers (market open/close, scheduled reevaluation)
- Ledger state (drawdown, open-position age, etc.)

Form: a JSON-expressible predicate tree. An in-project DSL may emerge once a few real strategies are in; until then, a permissive dict schema.

### 4.2 The observation schema (dynamic IR)

The **IR** (intermediate representation) is not a rulebook — it is the **structured observation** the Strands agent reasons over when woken. It is dynamic: fields materialize based on what the strategy prompt actually needs.

- Options strategy → IR includes full chain snapshots, greeks per leg, IV surface data.
- Turtle-trading equities strategy → IR includes N-day highs/lows, ATR, position sizing state.
- Crypto momentum strategy → IR includes order-book depth, funding rate, 1s candles.

The compilation agent emits a **schema** for the IR at strategy ingest time; the orchestrator materializes the schema into concrete data on each wake-up.

### 4.3 The decision loop (not compiled)

Unlike the TTA predicates, the **decision logic** stays LLM-driven. When woken, the Strategy Bot receives:

1. The original strategy prompt
2. The current IR snapshot
3. The bot's ledger state
4. Recent decision history (via AgentCore Memory)

…and returns a **trade intent** (buy/sell/close/hold, size, instrument, rationale).

Why not compile the decision logic to code as well? Because the interesting strategies — the ones where LLM reasoning adds value — cannot be cleanly expressed as rules. For purely mechanical strategies, we accept that we are paying for overkill and may revisit this later.

---

## 5. Architecture — the trading desk model

TradingStrands mirrors the topology of a real-world trading desk:

```
           Orchestrator (tick loop, TTA eval)
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
  Strategy Bot A  Strategy Bot B ... Strategy Bot N
       │              │              │
       └──────────────┼──────────────┘
                      ▼
             Trade Coordinator
                      │
                      ▼ (async dispatch)
              Risk Manager ◄── (advisory LLM agent, out of hot path)
                      │
                      ▼
                Broker Adapter
                      │
                      ▼
                Real Market

        Auditor (periodic, LLM agent)
            │
            ├── reads: Ledger (all bots)
            ├── reads: Broker Adapter (account state, actual fees)
            ├── owns: independent fee schedule
            └── authority: kill-switch (on drift threshold breach)
```

### 5.1 Orchestrator

A single process that runs a tick loop. Responsibilities:

- Pull market data for all instruments any active strategy cares about
- Evaluate TTA predicates for each active strategy against the new data
- When a TTA crosses, enqueue a wake-up event for that Strategy Bot
- Maintain heartbeat / health tracking for all downstream components

Tick cadence is **runtime-configurable** with defaults per asset class:

- **Equities / Options:** 5s
- **Crypto (BTC / ETH):** 1s

The orchestrator does not make trading decisions. It only wakes bots.

### 5.2 Strategy Bot

One per running strategy. A Strands agent that:

- Holds its strategy prompt and compiled IR schema
- Owns its ledger
- Owns its per-strategy risk tolerance (e.g., "never more than 2% equity on one position")
- Emits trade intents to the Trade Coordinator when woken
- Can receive kill-switch commands from the Risk Manager

### 5.3 Trade Coordinator

The pipeline between bots and execution. Responsibilities:

- Accept trade intents from Strategy Bots (async)
- Normalize intents into a canonical order format
- Dispatch to the Risk Manager for approval
- Route approved orders to the Broker Adapter
- Feed back fills, rejections, and execution reports to the originating bot's ledger

### 5.4 Risk Manager

**Deterministic code in the hot path.** Not an LLM. Responsibilities:

- Per-strategy risk limit enforcement (delegated from the bot's own tolerance)
- Portfolio-level exposure limits (correlated risk across bots)
- Drawdown tracking (per bot + aggregate)
- Kill-switch authority — can halt, liquidate, or restrict any bot or the whole desk
- Predictive limits — acts *before* a hard cap is breached, not after

An **advisory LLM agent** runs alongside the risk manager (outside the hot path). It:

- Explains risk events in natural language
- Spots cross-strategy patterns (e.g., "bots A and C are both long vol — correlated exposure growing")
- Proposes limit adjustments to the human operator
- Does **not** have trade-blocking authority — only the deterministic rules do

### 5.5 Broker Adapter

Pluggable interface over concrete broker APIs. The broker adapter defines a **common abstract interface** that all broker implementations must satisfy, ensuring brokers are swappable without changes to upstream components (strategy bots, coordinator, risk manager, auditor).

Planned implementations:

- **v0: Alpaca** via official SDK. Equities + options + crypto (Alpaca Crypto). Primary development and testing target.
- **v1: Robinhood** via `robin-stocks` (unofficial). Covers equities, options, BTC/ETH.
- **vN: additional brokers** — the abstract interface is designed to accommodate future broker integrations without architectural changes.

Each broker adapter implementation:

- Implements the common broker interface (order submission, fill reporting, position/account queries, market data passthrough)
- Maintains an authoritative **fee schedule** for that broker (commission rates, regulatory fee rates, options per-contract fees, etc.)
- Exposes the fee schedule to strategy bots for pre-trade cost estimation
- Reports the actual fees charged on each fill back to the ledger
- Decomposes fees into the granular categories defined in §3.4

⚠️ Robinhood has no official API. The unofficial library requires username/password + MFA flow and can break on any Robinhood client update. This is an accepted risk for v1 but is a known weak point.

### 5.6 Auditor

A **periodic LLM agent** that independently validates system integrity. The auditor runs on a configurable schedule (not in the tick-loop hot path) and produces natural-language audit reports.

#### 5.6.1 Responsibilities

- **Ledger-broker reconciliation** — compare each bot's virtual positions and balances against actual broker account state
- **Fee reconciliation** — independently calculate expected fees using its own fee schedule (maintained separately from the broker adapter's) and compare against fees actually charged by the broker
- **Order history integrity** — verify that every trade intent has a corresponding fill or rejection, and that no unexpected fills exist
- **Cross-strategy consistency** — check that the sum of all bot ledgers is consistent with aggregate broker account state

#### 5.6.2 Fee schedule independence

The auditor maintains its **own copy** of each broker's fee schedule, independent of the broker adapter's. This deliberate redundancy means:

- Fee calculation bugs in the broker adapter are caught by the auditor (and vice versa)
- Broker fee changes that are updated in one place but not the other surface as reconciliation failures
- Neither component trusts the other's math

#### 5.6.3 Kill-switch authority

The auditor has kill-switch capability gated by a **user-configurable drift threshold**. Examples:

- Fee discrepancy exceeds $X or Y% of trade value → `halt-and-stop-trading`
- Ledger-broker position mismatch persists across N audit cycles → `halt-and-stop-trading`
- Aggregate fee drift exceeds threshold over a rolling window → `halt-and-stop-trading`

Drift thresholds are set by the operator at deploy time and can be adjusted at runtime.

#### 5.6.4 Audit reports

Each audit cycle produces a structured report covering:

- Reconciliation status (pass/fail per check)
- Fee breakdown and discrepancies (if any)
- Drift trend over recent audit cycles
- Natural-language summary and recommendations (LLM-generated)

### 5.7 Market data

Multi-source:

- **Broker-provided quotes** (Robinhood / Alpaca) for trade-adjacent data
- **Yahoo Finance** (via `yfinance`) for historical context, fundamentals
- **Google Finance** for redundancy / cross-check (no official API; scraping-based)

The orchestrator aggregates these into a unified market snapshot consumed by TTA evaluation and IR materialization.

---

## 6. Kill switches

Multi-layered, with distinct verbs:

### 6.1 Kill-switch verbs

- **`halt-and-stop-trading`** — no new orders, existing positions held as-is.
- **`halt-and-liquidate-positions`** — flatten everything immediately via market orders.
- **`halt-and-sell-gains`** — close winning positions at market, hold losers (partial de-risk).
- **`halt-and-hedge`** — (future) open offsetting positions rather than close.
- **`pause-and-review`** — (future) human operator gate before any further decisions.

### 6.2 Authority layers

1. **Strategy Bot** — self-halt based on own risk tolerance.
2. **Risk Manager** — halt any single bot based on per-strategy deterministic rules.
3. **Risk Manager portfolio-level** — halt multiple bots or the entire desk based on aggregate exposure.
4. **Auditor** — halt any bot or the entire desk when drift thresholds are breached (user-configurable).
5. **Human operator** — always authoritative, always overrides everything.

### 6.3 Predictive halts

The Risk Manager should halt **before** a hard cap is breached, not after. Example: if the hard drawdown cap is 15% and a bot is at 12% with increasing velocity, the risk manager issues `halt-and-stop-trading` *now*, not at 15%. Exact predictive rules TBD during implementation.

---

## 7. Circuit breakers

Automatic tripwires distinct from kill switches. When any of the following fire, the whole desk enters a safe state (`halt-and-stop-trading` at minimum):

- Ledger / broker state divergence (virtual position says X, broker says Y)
- Market data feed staleness exceeds threshold
- Orchestrator tick lag exceeds threshold
- Unexpected rate-limit or auth failure from the broker
- An LLM decision call times out repeatedly
- Loss rate across bots exceeds aggregate threshold within a window
- Auditor detects fee discrepancy or ledger-broker drift exceeding user-configured thresholds

---

## 8. Runtime target

**Bedrock AgentCore from day one.**

- Strategy Bots run as Strands agents on Bedrock.
- AgentCore Memory holds per-bot decision history and context.
- AgentCore Identity handles broker credential scoping.
- AgentCore Gateway is deferred — direct SDK calls to broker APIs for v0.

Local dev loop: bots run against AgentCore-hosted models, orchestrator runs locally during development.

---

## 9. Repository layout

Single monorepo, single Python package with subpackages:

```
TradingStrands/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── .gitignore
├── pyproject.toml
├── docs/
│   └── SPEC.md              ← this document
├── src/
│   └── trading_strands/
│       ├── __init__.py
│       ├── orchestrator/    ← tick loop + TTA evaluation
│       ├── ir/              ← IR schema + compilation
│       ├── strategies/      ← Strategy Bot (Strands agent)
│       ├── coordinator/     ← Trade Coordinator
│       ├── risk/            ← Risk Manager (deterministic + advisory)
│       ├── broker/          ← broker adapters (robinhood, alpaca)
│       ├── marketdata/      ← yfinance, google finance, broker quotes
│       ├── ledger/          ← per-bot balance sheet + fee ledger
│       ├── whatif/          ← counterfactual tracker
│       └── auditor/         ← periodic reconciliation agent
├── examples/
│   └── strategies/          ← .md strategy files
└── tests/
```

Services are not split across processes until the process boundaries actually hurt.

---

## 10. Open questions (scoping debts)

Items that still need decisions before implementation:

1. **What-if exit logic** — when does a rejected trade stop being tracked? (Same exit rules as the strategy would've used? Fixed TTL? Track forever?)
2. **TTA predicate language** — stay in permissive dict form, or crystallize into a small DSL? Defer until we've ingested three or four real strategies.
3. **Predictive halt rules** — precise formulas for the risk manager's pre-cap halts.
4. **Correlation limits** — how does the risk manager measure portfolio-level correlated exposure across strategies?
5. **Robinhood auth flow** — (deferred to v1) MFA storage, session rotation, what to do when the unofficial API breaks.
6. **Observability / ops** — how the operator sees what the desk is doing in real time (TUI? webchat?).
7. **Audit log** — every decision, every trade, every kill switch, every risk check must be logged in a form suitable for post-incident review. Format TBD. The Auditor (§5.6) consumes this log — its format must support the auditor's reconciliation needs.
8. **Auditor cadence** — how frequently should the auditor run? Per-tick is too expensive (LLM agent); daily may be too slow to catch fee drift. Configurable, but what's the sensible default?
9. **Auditor fee schedule maintenance** — how are the auditor's independent fee schedules kept current? Manual operator update? Scraped from broker documentation? Versioned in config?
10. **Drift threshold defaults** — what are sensible default drift thresholds for the auditor's kill-switch authority? Need calibration against real broker fee variance.

---

## 11. Non-goals

Explicit non-features, to prevent scope creep:

- ❌ Backtesting (philosophical)
- ❌ Paper trading (philosophical)
- ❌ Market-making strategies (different architectural needs)
- ❌ HFT-latency execution (the orchestrator tick model precludes it)
- ❌ Strategies in languages other than prompt-markdown
- ❌ Fund-management features (multi-user accounts, reporting to LPs)

These may be reconsidered later but are out of scope for v0 / v1.
