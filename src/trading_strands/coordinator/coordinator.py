"""Trade coordinator — pipeline between bots and execution (§5.3)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from trading_strands.broker.types import OrderRequest, OrderResult, OrderType
from trading_strands.coordinator.types import (
    IntentAction,
    RiskDecision,
    RiskVerdict,
    TradeIntent,
    intent_to_side,
)
from trading_strands.ledger.models import Fill, Ledger
from trading_strands.risk.manager import RiskManager


class ExecutionResult(BaseModel):
    """Result of processing a trade intent through the full pipeline."""

    intent: TradeIntent
    risk_decision: RiskDecision | None = None
    order_result: OrderResult | None = None

    @property
    def approved(self) -> bool:
        if self.risk_decision is None:
            return self.order_result is not None
        return self.risk_decision.approved


class TradeCoordinator:
    """Accepts trade intents, runs risk checks, routes to broker, updates ledgers."""

    def __init__(
        self,
        broker: Any,  # BrokerAdapter protocol
        risk_manager: RiskManager,
        ledgers: dict[str, Ledger],
    ) -> None:
        self.broker = broker
        self.risk_manager = risk_manager
        self.ledgers = ledgers

    async def execute(self, intent: TradeIntent) -> ExecutionResult:
        """Process a trade intent through the full pipeline.

        Intent → risk check → broker execution → ledger update.
        """
        if intent.bot_id not in self.ledgers:
            msg = f"unknown bot: {intent.bot_id}"
            raise ValueError(msg)

        # Hold is a no-op
        if intent.action == IntentAction.HOLD:
            return ExecutionResult(
                intent=intent,
                risk_decision=RiskDecision(
                    verdict=RiskVerdict.APPROVED,
                    intent=intent,
                ),
            )

        ledger = self.ledgers[intent.bot_id]

        # Fetch market prices for risk evaluation
        market_prices = await self._get_market_prices(intent, ledger)

        # Risk check
        risk_decision = self.risk_manager.evaluate(intent, ledger, market_prices)
        if not risk_decision.approved:
            return ExecutionResult(
                intent=intent,
                risk_decision=risk_decision,
            )

        # Convert intent to order and submit
        order = self._intent_to_order(intent)
        order_result = await self.broker.submit_order(order)

        # Record fill in ledger
        self._record_fill(intent, order_result, ledger)

        return ExecutionResult(
            intent=intent,
            risk_decision=risk_decision,
            order_result=order_result,
        )

    async def _get_market_prices(
        self, intent: TradeIntent, ledger: Ledger,
    ) -> dict[str, Decimal]:
        """Fetch current prices for the intent symbol and all open positions."""
        symbols = {intent.symbol}
        symbols.update(pos.symbol for pos in ledger.open_positions)

        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            quote = await self.broker.get_quote(symbol)
            price = quote.get("price")
            if isinstance(price, Decimal):
                prices[symbol] = price
            elif isinstance(price, (int, float, str)):
                prices[symbol] = Decimal(str(price))
        return prices

    def _intent_to_order(self, intent: TradeIntent) -> OrderRequest:
        """Normalize a trade intent into a canonical order."""
        return OrderRequest(
            symbol=intent.symbol,
            side=intent_to_side(intent.action),
            quantity=intent.quantity,
            order_type=OrderType.MARKET,
        )

    def _record_fill(
        self, intent: TradeIntent, result: OrderResult, ledger: Ledger,
    ) -> None:
        """Record a broker fill into the bot's ledger."""
        if result.filled_quantity <= 0:
            return
        fill = Fill(
            symbol=intent.symbol,
            side=intent_to_side(intent.action),
            quantity=result.filled_quantity,
            price=result.filled_price,
            fees=result.fees,
        )
        ledger.record_fill(fill)
