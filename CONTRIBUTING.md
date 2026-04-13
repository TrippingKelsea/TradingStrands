# Contributing to TradingStrands

Thanks for your interest. TradingStrands is a project where **real money is at stake by design** — contributions are welcome, but held to a higher bar than most side projects.

## Before you start

Read [`docs/SPEC.md`](docs/SPEC.md) in full. In particular, internalize the core philosophy:

> There is no environment like production to test what happens in production.

This project deliberately does not ship backtesting or paper-trading modes. Proposals to add them will be declined. The compensating mechanism is the what-if counterfactual tracker — improvements there are welcome.

## Development setup

Requires Python 3.12+.

```bash
# Clone
git clone <repo> TradingStrands
cd TradingStrands

# Create venv and install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run checks
ruff check .
ruff format --check .
mypy src/
pytest
```

AWS credentials for Bedrock + AgentCore and broker credentials live in `.env` (never commit — see `.env.example`).

## Conventions

- **Python 3.12+**, type hints required on all public functions.
- **Ruff** for lint + format, **mypy** in strict mode for `src/`.
- **Tests**: unit tests for pure logic (IR compilation, ledger math, risk rules). The agent loop and broker adapters are tested against live APIs in a controlled account — there is no mock broker by design.
- Keep modules small and boring. The risk manager is deterministic code, not an LLM — do not regress this.
- New strategies go under `examples/strategies/` as `.md` files and are not part of the core library.

## Pull requests

- One logical change per PR.
- Include a short rationale in the PR description — especially anything that touches the risk manager, ledger, or kill-switch paths.
- PRs touching order execution or risk logic require an extra reviewer.

## Risk-sensitive code

Any change to the following paths must include a description of how it was validated and what the blast radius is if it's wrong:

- `src/trading_strands/risk/`
- `src/trading_strands/coordinator/`
- `src/trading_strands/broker/`
- `src/trading_strands/ledger/`

These paths gate real-money behavior. "It compiles and the tests pass" is not sufficient justification.

## Reporting issues

Security issues (credential handling, key exposure, auth bypass in the broker layer) — do not open a public issue. Contact the maintainer directly.
