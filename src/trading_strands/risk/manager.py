"""Deterministic risk manager (§5.4).

This is the hot-path risk gate. All logic here is deterministic code —
never LLM calls. Changes to this module require extra validation.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from trading_strands.coordinator.types import (
    IntentAction,
    RiskDecision,
    RiskVerdict,
    TradeIntent,
)
from trading_strands.ledger.models import Ledger


class RiskConfig(BaseModel):
    """Configurable risk limits."""

    max_position_pct: Decimal = Decimal("0.20")  # max single position as % of equity
    max_total_exposure_pct: Decimal = Decimal("0.80")  # max total exposure as % of equity
    max_drawdown_pct: Decimal = Decimal("0.15")  # max drawdown from HWM
    daily_loss_cap_pct: Decimal = Decimal("0.05")  # max daily loss as % of equity


class RiskManager:
    """Deterministic risk manager — evaluates trade intents against limits."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._halted_bots: set[str] = set()
        self._desk_halted: bool = False
        self._daily_loss: Decimal = Decimal("0")

    def halt_bot(self, bot_id: str) -> None:
        self._halted_bots.add(bot_id)

    def unhalt_bot(self, bot_id: str) -> None:
        self._halted_bots.discard(bot_id)

    def halt_desk(self) -> None:
        self._desk_halted = True

    def unhalt_desk(self) -> None:
        self._desk_halted = False

    def record_daily_loss(self, amount: Decimal) -> None:
        self._daily_loss += amount

    def reset_daily_loss(self) -> None:
        self._daily_loss = Decimal("0")

    def evaluate(
        self,
        intent: TradeIntent,
        ledger: Ledger,
        market_prices: dict[str, Decimal],
    ) -> RiskDecision:
        """Evaluate a trade intent against all risk rules.

        Returns an approved or rejected decision with reason.
        """
        # Hold is always a no-op
        if intent.action == IntentAction.HOLD:
            return self._approve(intent)

        # Kill-switch checks first
        if self._desk_halted:
            return self._reject(intent, "desk is halted")
        if intent.bot_id in self._halted_bots:
            return self._reject(intent, f"bot {intent.bot_id} is halted")

        # Sells/closes are always allowed (we want to let positions be reduced)
        if intent.action in (IntentAction.SELL, IntentAction.CLOSE):
            return self._approve(intent)

        # All remaining checks apply to buys only
        equity = ledger.equity

        # Drawdown check
        if ledger.drawdown_pct > self._config.max_drawdown_pct:
            return self._reject(
                intent,
                f"drawdown {ledger.drawdown_pct:.2%} exceeds "
                f"limit {self._config.max_drawdown_pct:.2%}",
            )

        # Daily loss cap
        if equity > 0:
            daily_loss_pct = self._daily_loss / equity
            if daily_loss_pct > self._config.daily_loss_cap_pct:
                return self._reject(
                    intent,
                    f"daily loss {daily_loss_pct:.2%} exceeds "
                    f"limit {self._config.daily_loss_cap_pct:.2%}",
                )

        # Per-position size check
        if intent.symbol in market_prices:
            position_value = intent.quantity * market_prices[intent.symbol]
            position_pct = position_value / equity if equity > 0 else Decimal("1")
            if position_pct > self._config.max_position_pct:
                return self._reject(
                    intent,
                    f"position size {position_pct:.2%} of equity exceeds "
                    f"limit {self._config.max_position_pct:.2%}",
                )

        # Total exposure check
        current_exposure = sum(
            pos.quantity * market_prices.get(pos.symbol, pos.burdened_cost_basis)
            for pos in ledger.open_positions
        )
        new_exposure = current_exposure
        if intent.symbol in market_prices:
            new_exposure += intent.quantity * market_prices[intent.symbol]
        if equity > 0:
            exposure_pct = new_exposure / equity
            if exposure_pct > self._config.max_total_exposure_pct:
                return self._reject(
                    intent,
                    f"total exposure {exposure_pct:.2%} exceeds "
                    f"limit {self._config.max_total_exposure_pct:.2%}",
                )

        return self._approve(intent)

    def _approve(self, intent: TradeIntent) -> RiskDecision:
        return RiskDecision(
            verdict=RiskVerdict.APPROVED,
            intent=intent,
        )

    def _reject(self, intent: TradeIntent, reason: str) -> RiskDecision:
        return RiskDecision(
            verdict=RiskVerdict.REJECTED,
            intent=intent,
            reason=reason,
        )
