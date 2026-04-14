"""Strategy compilation agent (§4).

Ingests a markdown strategy document and extracts:
1. TTA predicates (§4.1) — conditions for waking the bot
2. IR schema (§4.2) — observation fields the bot needs
3. Strategy metadata — symbols, capital, risk parameters

This is an LLM-powered compilation step that runs once at strategy
load time, not in the hot path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import structlog
from pydantic import BaseModel
from strands import Agent

from trading_strands.ir.tta import Predicate
from trading_strands.risk.manager import RiskConfig

logger = structlog.get_logger()


class IRField(BaseModel):
    """A field in the observation schema."""

    name: str
    description: str
    source: str  # e.g., "market_data", "ledger", "indicator"


class CompiledStrategy(BaseModel):
    """Output of strategy compilation."""

    symbols: list[str]
    starting_capital: Decimal
    tta_entry: dict[str, Any]  # TTA predicate for entry conditions
    tta_exit: dict[str, Any]  # TTA predicate for exit conditions
    ir_fields: list[IRField]
    risk_config: dict[str, Any]
    strategy_summary: str


_COMPILER_SYSTEM_PROMPT = """\
You are a trading strategy compiler. Your job is to analyze a natural-language \
strategy document and extract structured, machine-readable components.

You must extract:
1. **Symbols** — what instruments does this strategy trade?
2. **Starting capital** — what is the initial capital allocation?
3. **Entry conditions** — when should the bot consider buying? Express as \
conditions referencing price levels, indicators, or ledger state.
4. **Exit conditions** — when should the bot consider selling?
5. **IR fields** — what market data and indicators does the bot need to make \
decisions? Each field needs a name, description, and data source.
6. **Risk parameters** — drawdown caps, position sizing rules, daily loss limits.
7. **Summary** — a concise summary of the strategy.

Be precise. Extract exact numerical values from the strategy document. \
If a value is ambiguous, use the most conservative interpretation."""

_COMPILER_PROMPT_TEMPLATE = """\
Compile the following trading strategy into structured components:

---
{strategy_text}
---

Extract the symbols, capital, entry/exit conditions, required data fields, \
and risk parameters."""


async def compile_strategy(
    strategy_text: str,
    model: str | None = None,
) -> CompiledStrategy:
    """Compile a markdown strategy into structured components.

    This is an LLM-powered step that runs once at strategy load time.
    """
    agent = Agent(
        model=model,
        system_prompt=_COMPILER_SYSTEM_PROMPT,
    )

    prompt = _COMPILER_PROMPT_TEMPLATE.format(strategy_text=strategy_text)

    result = await agent.invoke_async(
        prompt,
        structured_output_model=CompiledStrategy,
    )

    compiled = result.structured_output
    if compiled is None:
        msg = "strategy compilation failed: no structured output"
        raise ValueError(msg)

    return cast(CompiledStrategy, compiled)


def compiled_to_risk_config(compiled: CompiledStrategy) -> RiskConfig:
    """Convert compiled risk parameters to a RiskConfig."""
    risk = compiled.risk_config
    return RiskConfig(
        max_position_pct=Decimal(str(risk.get("max_position_pct", "0.20"))),
        max_total_exposure_pct=Decimal(str(risk.get("max_total_exposure_pct", "0.80"))),
        max_drawdown_pct=Decimal(str(risk.get("max_drawdown_pct", "0.15"))),
        daily_loss_cap_pct=Decimal(str(risk.get("daily_loss_cap_pct", "0.05"))),
    )


def compiled_to_tta(compiled: CompiledStrategy) -> Predicate:
    """Build a combined TTA predicate from entry and exit conditions."""
    return {
        "or": [
            compiled.tta_entry,
            compiled.tta_exit,
        ],
    }
