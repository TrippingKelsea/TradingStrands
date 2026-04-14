# CLAUDE.md — TradingStrands

## Project overview

TradingStrands is a strategy-as-prompt trading agent framework. Natural language strategies are compiled into structured predicates (TTA) and observation schemas (IR), then executed as live agents against real markets under deterministic risk management.

**Authoritative design document:** `docs/SPEC.md` — if the code and spec disagree, the spec wins (or needs updating).

## Core philosophy

> There is no environment like production to test what happens in production.

Do NOT add backtesting, paper trading, or simulation modes. This is a deliberate design choice, not a gap. The compensating mechanism is the what-if counterfactual tracker.

## Workflow rules

### Commits
- **Commit after every small, logical change.** Do not batch unrelated changes. One concern per commit.
- Write commit messages that explain *why*, not just *what*.
- Always verify lint/typecheck/tests pass before committing: `uv run ruff check . && uv run mypy src/ && uv run pytest --tb=short -q`
- After pushing, check CI with `gh run list --limit 1` to confirm it passes.

### Test-driven development
- **Write tests before implementation code.** The test defines the contract; the implementation satisfies it.
- Tests go in `tests/`, mirroring the `src/trading_strands/` structure (e.g., `tests/test_ledger.py` for `src/trading_strands/ledger/`).
- Unit tests for pure logic (IR compilation, ledger math, fee calculations, risk rules). No mock brokers — broker adapters are tested against live APIs.
- Run tests with `uv run pytest --tb=short -q`.

## Toolchain

- **Package manager:** `uv` (not pip). Use `uv sync --extra dev` to install, `uv run` to execute tools.
- **Lint/format:** `ruff` — config in `pyproject.toml`.
- **Type checking:** `mypy` in strict mode on `src/`.
- **Testing:** `pytest` with `pytest-asyncio` (auto mode).
- **CI:** GitHub Actions (`.github/workflows/ci.yml`) — runs lint, typecheck, and tests on push/PR. Use `gh run list` and `gh run view` to check results.

## Code conventions

- Python 3.12+, type hints required on all public functions.
- `pydantic` for data models and validation.
- `structlog` for logging.
- `anyio` for async (not raw asyncio).
- Keep modules small. Prefer simple, boring code — especially in risk-critical paths.

## Risk-sensitive paths

These paths gate real-money behavior. Changes require extra care — describe how it was validated and what breaks if it's wrong:

- `src/trading_strands/risk/` — deterministic risk manager, kill switches
- `src/trading_strands/coordinator/` — trade routing and execution
- `src/trading_strands/broker/` — broker API adapters
- `src/trading_strands/ledger/` — balance sheets, fee tracking
- `src/trading_strands/auditor/` — reconciliation, drift-based kill switches

The risk manager is **deterministic code in the hot path** — never regress this to LLM calls.

## Architecture quick reference

```
Orchestrator (tick loop, TTA eval)
    → Strategy Bots (Strands agents, one per strategy)
        → Trade Coordinator (normalizes intents, routes)
            → Risk Manager (deterministic approval/rejection)
                → Broker Adapter (abstract interface, Alpaca v0)

Auditor (periodic LLM agent, independent fee validation, kill-switch authority)
```

## Key design decisions

- **Broker adapter is an abstract interface** — implementations must be swappable. Alpaca is v0, Robinhood v1.
- **Fees tracked at full granularity** — commission, regulatory (SEC/TAF/FINRA), options, crypto. Trades use fully-burdened cost basis for PnL; fees also available as separate line items.
- **Auditor maintains independent fee schedules** from the broker adapter — deliberate redundancy so neither trusts the other's math.
- **Multiple kill-switch layers** — strategy bot → risk manager → risk manager (portfolio) → auditor → human operator. Each layer can halt independently.
