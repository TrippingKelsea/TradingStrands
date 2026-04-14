"""Strategy bot — Strands agent wrapper for LLM-driven trade decisions (§5.2).

Each strategy bot:
- Holds its strategy prompt and compiled IR schema
- Owns its ledger
- Emits trade intents when woken by the orchestrator
- Can be halted by the risk manager
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import structlog
from pydantic import BaseModel
from strands import Agent

from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.ir.tta import Predicate
from trading_strands.ledger.models import Ledger

logger = structlog.get_logger()


class BotDecision(BaseModel):
    """Structured output from the strategy bot's LLM decision."""

    action: str  # buy, sell, close, hold
    symbol: str
    quantity: str  # string to avoid float precision issues
    rationale: str


_DECISION_PROMPT_TEMPLATE = """\
You are a trading strategy bot. Your job is to analyze the current market \
conditions and your portfolio state, then decide what action to take.

## Your Strategy
{strategy_prompt}

## Current Market Data
{market_data}

## Your Portfolio
{portfolio_state}

## Recent Decisions
{recent_decisions}

## Instructions
Based on your strategy rules and the current conditions, decide your next action.
- If conditions warrant a trade, specify the action (buy/sell/close), symbol, and quantity.
- If no action is needed, respond with action "hold".
- Always provide a clear rationale explaining your reasoning.
- Quantity should be a whole number of shares.
- Only trade symbols your strategy covers.
"""


def _format_market_data(prices: dict[str, Decimal]) -> str:
    if not prices:
        return "No market data available."
    lines = [f"  {symbol}: ${price}" for symbol, price in sorted(prices.items())]
    return "\n".join(lines)


def _format_portfolio(ledger: Ledger) -> str:
    lines = [
        f"Starting capital: ${ledger.starting_capital}",
        f"Equity: ${ledger.equity}",
        f"Realized PnL: ${ledger.realized_pnl}",
        f"High water mark: ${ledger.high_water_mark}",
        f"Drawdown: {ledger.drawdown_pct:.2%}",
    ]
    if ledger.open_positions:
        lines.append("Open positions:")
        for pos in ledger.open_positions:
            lines.append(
                f"  {pos.symbol}: {pos.quantity} shares @ "
                f"${pos.burdened_cost_basis} cost basis"
            )
    else:
        lines.append("No open positions.")
    return "\n".join(lines)


def _map_action(action_str: str) -> IntentAction:
    mapping = {
        "buy": IntentAction.BUY,
        "sell": IntentAction.SELL,
        "close": IntentAction.CLOSE,
        "hold": IntentAction.HOLD,
    }
    return mapping.get(action_str.lower(), IntentAction.HOLD)


class StrategyBot:
    """A strategy bot backed by a Strands agent.

    The bot is registered with the orchestrator and called on each tick
    where its TTA predicate fires. It uses an LLM to decide whether
    to trade, based on the strategy prompt and current conditions.
    """

    def __init__(
        self,
        bot_id: str,
        strategy_prompt: str,
        symbols: list[str],
        tta: Predicate | None = None,
        model: str | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.strategy_prompt = strategy_prompt
        self.symbols = symbols
        self.tta = tta
        self._recent_decisions: list[str] = []
        self._max_history = 10

        self._agent = Agent(
            model=model,
            system_prompt=(
                "You are a disciplined trading bot. Follow your strategy rules "
                "precisely. Never deviate from the strategy. Be conservative "
                "when uncertain — prefer to hold rather than make a bad trade."
            ),
        )

    async def decide(
        self,
        bot_id: str,
        prices: dict[str, Decimal],
        ledger: Ledger,
    ) -> TradeIntent | None:
        """Make a trading decision based on current conditions.

        This is called by the orchestrator on each tick where the TTA fires.
        Returns a TradeIntent or None (hold).
        """
        prompt = _DECISION_PROMPT_TEMPLATE.format(
            strategy_prompt=self.strategy_prompt,
            market_data=_format_market_data(prices),
            portfolio_state=_format_portfolio(ledger),
            recent_decisions=self._format_recent() or "No recent decisions.",
        )

        try:
            result = await self._agent.invoke_async(
                prompt,
                structured_output_model=BotDecision,
            )
        except Exception:
            await logger.aexception("bot.llm.error", bot_id=self.bot_id)
            return None

        raw_decision = result.structured_output
        if raw_decision is None:
            await logger.awarn("bot.no_decision", bot_id=self.bot_id)
            return None

        decision = cast(BotDecision, raw_decision)

        # Record decision for history
        self._recent_decisions.append(
            f"{decision.action} {decision.symbol} x{decision.quantity}: "
            f"{decision.rationale}"
        )
        if len(self._recent_decisions) > self._max_history:
            self._recent_decisions = self._recent_decisions[-self._max_history :]

        action = _map_action(decision.action)
        if action == IntentAction.HOLD:
            return None

        return TradeIntent(
            bot_id=self.bot_id,
            symbol=decision.symbol,
            action=action,
            quantity=Decimal(decision.quantity),
            rationale=decision.rationale,
        )

    def _format_recent(self) -> str:
        if not self._recent_decisions:
            return ""
        return "\n".join(
            f"  {i + 1}. {d}" for i, d in enumerate(self._recent_decisions)
        )
