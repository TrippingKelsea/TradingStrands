"""Trade coordinator — pipeline between bots and execution (§5.3).

The coordinator routes trade intents to the correct per-org broker. A bot
running under org A must execute its trades through org A's broker
credentials only; the coordinator enforces this by using `intent.org_id`
to select the broker rather than relying on a shared global broker.

Brokers are built lazily by a factory the caller supplies — we don't
spin up one broker per org at startup, since most orgs are idle most of
the time. On the first intent from org X, we build and cache org X's
broker; subsequent intents reuse that instance.

This is the v0 multi-org execution path. The v1 target (per-org Broker
Agent as separate Fargate service) is described in docs/SPEC/agents.md.
"""

from __future__ import annotations

from collections.abc import Callable
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

BrokerFactory = Callable[[str], Any]
"""Signature: (org_id) -> BrokerAdapter. Raises if creds unavailable."""


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


class BrokerUnavailableError(Exception):
    """Raised when the broker factory can't produce a broker for an org
    (e.g., the org has no Alpaca credentials configured)."""


class TradeCoordinator:
    """Accepts trade intents, runs risk checks, routes to per-org brokers,
    updates ledgers.

    Pass a `broker_factory(org_id)` callable; the coordinator caches
    produced brokers per org. If you need a platform-level broker for
    market-data-only reads, pass `default_broker` — the coordinator
    uses it for price fetches when no org-scoped broker is appropriate.
    """

    def __init__(
        self,
        broker_factory: BrokerFactory,
        risk_manager: RiskManager,
        ledgers: dict[str, Ledger],
        default_broker: Any | None = None,
        ledger_store: Any | None = None,
    ) -> None:
        self._broker_factory = broker_factory
        self._broker_cache: dict[str, Any] = {}
        self.risk_manager = risk_manager
        self.ledgers = ledgers
        self._default_broker = default_broker
        # Optional — in AWS mode this is a LedgerStore that persists every
        # fill to DynamoDB. Tests and local-dev pass None; the coordinator
        # still functions (just without durability).
        self._ledger_store = ledger_store

    def broker_for(self, org_id: str) -> Any:
        """Return the broker adapter for this org, creating it on first use.

        Errors from the factory are wrapped as BrokerUnavailableError so
        callers can handle missing creds as a routable failure rather than
        a generic exception. Once a broker is cached, subsequent calls are
        a plain dict lookup.
        """

        cached = self._broker_cache.get(org_id)
        if cached is not None:
            return cached
        try:
            broker = self._broker_factory(org_id)
        except Exception as exc:
            msg = f"broker unavailable for org {org_id}: {exc}"
            raise BrokerUnavailableError(msg) from exc
        self._broker_cache[org_id] = broker
        return broker

    def invalidate_broker(self, org_id: str) -> None:
        """Drop the cached broker for an org. Called when credentials
        rotate — next broker_for() rebuilds from the factory."""

        self._broker_cache.pop(org_id, None)

    async def execute(self, intent: TradeIntent) -> ExecutionResult:
        """Process a trade intent through the full pipeline.

        Intent → per-org broker → risk check → broker execution → ledger update.
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

        # Resolve the broker for this org BEFORE risk-checking. If the org
        # can't produce a broker (missing creds), reject the intent up-front
        # rather than running risk checks that will be wasted work.
        try:
            broker = self.broker_for(intent.org_id)
        except BrokerUnavailableError as exc:
            return ExecutionResult(
                intent=intent,
                risk_decision=RiskDecision(
                    verdict=RiskVerdict.REJECTED,
                    intent=intent,
                    reason=str(exc),
                ),
            )

        ledger = self.ledgers[intent.bot_id]

        # Fetch market prices for risk evaluation
        market_prices = await self._get_market_prices(broker, intent, ledger)

        # Risk check
        risk_decision = self.risk_manager.evaluate(intent, ledger, market_prices)
        if not risk_decision.approved:
            return ExecutionResult(
                intent=intent,
                risk_decision=risk_decision,
            )

        # Convert intent to order and submit
        order = self._intent_to_order(intent)
        order_result = await broker.submit_order(order)

        # Record fill in ledger
        self._record_fill(intent, order_result, ledger)

        return ExecutionResult(
            intent=intent,
            risk_decision=risk_decision,
            order_result=order_result,
        )

    async def _get_market_prices(
        self, broker: Any, intent: TradeIntent, ledger: Ledger,
    ) -> dict[str, Decimal]:
        """Fetch current prices for the intent symbol and all open positions.

        Uses the org-scoped broker. Price data via Alpaca is the same across
        credentials (market data isn't per-account-segregated), but using
        the org's broker keeps rate-limit accounting clean and auditable."""

        symbols = {intent.symbol}
        symbols.update(pos.symbol for pos in ledger.open_positions)

        prices: dict[str, Decimal] = {}
        for symbol in symbols:
            quote = await broker.get_quote(symbol)
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
        """Record a broker fill into the bot's ledger AND persist.

        When a ledger_store is configured (production), this uses the
        store's record_and_persist helper: apply fill in-memory → append
        event → save snapshot. See LedgerStore for crash-recovery
        semantics. Without a store (tests / local-dev), just updates
        in-memory state — the prior v0 behavior.
        """

        if result.filled_quantity <= 0:
            return
        fill = Fill(
            symbol=intent.symbol,
            side=intent_to_side(intent.action),
            quantity=result.filled_quantity,
            price=result.filled_price,
            fees=result.fees,
        )
        if self._ledger_store is None:
            ledger.record_fill(fill)
        else:
            self._ledger_store.record_and_persist(intent.bot_id, ledger, fill)
