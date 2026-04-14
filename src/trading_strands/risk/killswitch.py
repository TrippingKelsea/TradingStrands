"""Kill-switch verb implementations (§6.1).

Multi-layered kill switches with distinct verbs:
- halt-and-stop-trading: no new orders, positions held as-is
- halt-and-liquidate-positions: flatten everything via market orders
- halt-and-sell-gains: close winners, hold losers
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog

from trading_strands.broker.types import OrderRequest, OrderType
from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.ledger.models import Side
from trading_strands.risk.manager import RiskManager

logger = structlog.get_logger()


class KillSwitchVerb(StrEnum):
    HALT_AND_STOP = "halt-and-stop-trading"
    HALT_AND_LIQUIDATE = "halt-and-liquidate-positions"
    HALT_AND_SELL_GAINS = "halt-and-sell-gains"


class KillSwitch:
    """Executes kill-switch verbs against the trading system."""

    def __init__(
        self,
        coordinator: TradeCoordinator,
        risk_manager: RiskManager,
    ) -> None:
        self._coordinator = coordinator
        self._risk_manager = risk_manager
        self.log: list[dict[str, Any]] = []

    async def execute(
        self,
        verb: KillSwitchVerb,
        bot_id: str | None = None,
        market_prices: dict[str, Decimal] | None = None,
    ) -> None:
        """Execute a kill-switch verb.

        If bot_id is None, applies to the entire desk.
        """
        self._log_event(verb, bot_id)

        if verb == KillSwitchVerb.HALT_AND_STOP:
            await self._halt_and_stop(bot_id)
        elif verb == KillSwitchVerb.HALT_AND_LIQUIDATE:
            await self._halt_and_liquidate(bot_id)
        elif verb == KillSwitchVerb.HALT_AND_SELL_GAINS:
            await self._halt_and_sell_gains(bot_id, market_prices or {})

    async def _halt_and_stop(self, bot_id: str | None) -> None:
        """No new orders, existing positions held as-is."""
        if bot_id:
            self._risk_manager.halt_bot(bot_id)
        else:
            self._risk_manager.halt_desk()

    async def _halt_and_liquidate(self, bot_id: str | None) -> None:
        """Flatten all positions via market orders, then halt."""
        target_ledgers = self._get_target_ledgers(bot_id)

        for _lid, ledger in target_ledgers.items():
            for pos in list(ledger.open_positions):
                order = OrderRequest(
                    symbol=pos.symbol,
                    side=Side.SELL,
                    quantity=pos.quantity,
                    order_type=OrderType.MARKET,
                )
                result = await self._coordinator.broker.submit_order(order)
                # Record the fill in the ledger
                from trading_strands.ledger.models import Fill

                if result.filled_quantity > 0:
                    fill = Fill(
                        symbol=pos.symbol,
                        side=Side.SELL,
                        quantity=result.filled_quantity,
                        price=result.filled_price,
                        fees=result.fees,
                    )
                    ledger.record_fill(fill)

        # Then halt
        if bot_id:
            self._risk_manager.halt_bot(bot_id)
        else:
            self._risk_manager.halt_desk()

    async def _halt_and_sell_gains(
        self, bot_id: str | None, market_prices: dict[str, Decimal],
    ) -> None:
        """Close winning positions, hold losers, then halt."""
        target_ledgers = self._get_target_ledgers(bot_id)

        for _lid, ledger in target_ledgers.items():
            for pos in list(ledger.open_positions):
                current_price = market_prices.get(pos.symbol)
                if current_price is None:
                    continue

                # Only sell if position is profitable
                if current_price > pos.burdened_cost_basis:
                    order = OrderRequest(
                        symbol=pos.symbol,
                        side=Side.SELL,
                        quantity=pos.quantity,
                        order_type=OrderType.MARKET,
                    )
                    result = await self._coordinator.broker.submit_order(order)

                    from trading_strands.ledger.models import Fill

                    if result.filled_quantity > 0:
                        fill = Fill(
                            symbol=pos.symbol,
                            side=Side.SELL,
                            quantity=result.filled_quantity,
                            price=result.filled_price,
                            fees=result.fees,
                        )
                        ledger.record_fill(fill)

        # Then halt
        if bot_id:
            self._risk_manager.halt_bot(bot_id)
        else:
            self._risk_manager.halt_desk()

    def _get_target_ledgers(self, bot_id: str | None) -> dict[str, Any]:
        if bot_id:
            ledger = self._coordinator.ledgers.get(bot_id)
            if ledger:
                return {bot_id: ledger}
            return {}
        return dict(self._coordinator.ledgers)

    def _log_event(self, verb: KillSwitchVerb, bot_id: str | None) -> None:
        self.log.append({
            "verb": verb,
            "bot_id": bot_id,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        })
