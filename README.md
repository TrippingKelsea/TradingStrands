# TradingStrands

Strategy-as-prompt trading agents built on AWS Strands + Bedrock AgentCore.

## What it is

TradingStrands lets you describe a trading strategy in natural language — anything from "use turtle trading methodology with $1000" to multi-page theory-driven specs — and runs it as a live, risk-managed agent against real markets.

Strategies are compiled into a structured IR (threshold-to-action predicates + dynamic market-state observation schema) and executed by per-strategy agents under the supervision of a deterministic risk manager, modeled on a real-world trading desk.

## Core philosophy

> There is no environment like production to test what happens in production.

No backtesting. No paper trading. Real money, real stakes, real markets — compensated by a what-if counterfactual tracker that records both trades taken and trades passed, marking them to market over time.

## Architecture at a glance

```
┌─────────────────┐
│  Orchestrator   │  ticks every 1s (crypto) / 5s (equities, options)
│   (TTA check)   │  wakes strategies on threshold crossings
└────────┬────────┘
         │
    ┌────┴──────────────┬──────────────────┐
    ▼                   ▼                  ▼
┌─────────┐        ┌─────────┐        ┌─────────┐
│ Strategy│        │ Strategy│  ...   │ Strategy│   each: Strands agent,
│  Bot A  │        │  Bot B  │        │  Bot N  │   own ledger, own risk
└────┬────┘        └────┬────┘        └────┬────┘   tolerance, own TTA
     │ intent           │ intent           │ intent
     └──────────┬───────┴──────────────────┘
                ▼
        ┌───────────────┐
        │     Trade     │   normalizes & routes intents
        │  Coordinator  │   async dispatch to risk mgr
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ Risk Manager  │   deterministic rules in hot path
        │  (oversight)  │   LLM advisory alongside
        └───────┬───────┘   kill-switch authority
                ▼
        ┌───────────────┐
        │Broker Adapter │   Robinhood (v0), Alpaca (v1)
        └───────────────┘
```

## Supported markets

- **Equities** — Robinhood (v0), Alpaca (v1)
- **Options** — same brokers; greeks are first-class in the IR
- **Crypto** — BTC / ETH only

## Status

Pre-alpha. See [`docs/SPEC.md`](docs/SPEC.md) for the authoritative design document.

## License

MIT. See [`LICENSE`](LICENSE).
