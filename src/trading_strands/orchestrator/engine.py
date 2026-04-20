"""Orchestrator — tick loop that evaluates TTA predicates and wakes bots (§5.1)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import anyio
import structlog

from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.dashboard.publisher import StatePublisher
from trading_strands.ir.tta import Context, Predicate, evaluate
from trading_strands.ledger.models import Ledger
from trading_strands.marketdata.provider import MarketDataProvider

logger = structlog.get_logger()


class BotRegistration:
    """A registered bot with its TTA predicate and decision callback."""

    def __init__(
        self,
        bot_id: str,
        symbols: list[str],
        callback: BotCallback,
        tta: Predicate | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.symbols = symbols
        self.callback = callback
        self.tta = tta


class Orchestrator:
    """Runs the tick loop, evaluates TTA predicates, and dispatches to bots.

    Each tick:
    1. Fetches market data for all watched symbols
    2. Builds a TTA evaluation context (prices + ledger state)
    3. Evaluates each bot's TTA predicate — only wakes bots whose predicates fire
    4. Calls woken bot's decide() callback with the market snapshot
    5. Routes any resulting trade intents through the coordinator
    """

    def __init__(
        self,
        coordinator: TradeCoordinator,
        market_data: MarketDataProvider,
        tick_interval: float = 5.0,
        publisher: StatePublisher | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.market_data = market_data
        self.tick_interval = tick_interval
        self._publisher = publisher
        self._bots: dict[str, BotRegistration] = {}
        self._symbols: set[str] = set()
        self._running = False
        self._prev_ctx: Context | None = None

    def register_bot(
        self,
        bot_id: str,
        symbols: list[str],
        callback: BotCallback,
        tta: Predicate | None = None,
    ) -> None:
        """Register a bot with its watched symbols, TTA predicate, and callback.

        If tta is None, the bot is woken on every tick.
        """
        self._bots[bot_id] = BotRegistration(
            bot_id=bot_id,
            symbols=symbols,
            callback=callback,
            tta=tta,
        )
        self._symbols.update(symbols)

    def unregister_bot(self, bot_id: str) -> None:
        """Remove a bot and recalculate watched symbols."""
        self._bots.pop(bot_id, None)
        self._symbols = set()
        for reg in self._bots.values():
            self._symbols.update(reg.symbols)

    async def run(self, max_ticks: int | None = None) -> None:
        """Run the tick loop.

        Args:
            max_ticks: Stop after this many ticks (None = run forever).
        """
        self._running = True
        tick = 0

        await logger.ainfo("orchestrator.start", bots=list(self._bots.keys()),
                           symbols=list(self._symbols))

        while self._running:
            if max_ticks is not None and tick >= max_ticks:
                break

            await self._tick(tick)
            tick += 1

            if self._running and (max_ticks is None or tick < max_ticks):
                await anyio.sleep(self.tick_interval)

        await logger.ainfo("orchestrator.stop", total_ticks=tick)

    async def _tick(self, tick_number: int) -> None:
        """Execute a single tick."""
        if not self._bots:
            # Still publish snapshot so dashboard shows connected state
            self._publish_snapshot(tick_number, {})
            return

        # 1. Fetch market data
        prices = await self.market_data.get_prices(self._symbols)

        await logger.adebug("orchestrator.tick", tick=tick_number,
                            prices={s: str(p) for s, p in prices.items()})

        # 2. Wake each bot and collect intents
        for bot_id, reg in list(self._bots.items()):
            ledger = self.coordinator.ledgers.get(bot_id)
            if ledger is None:
                continue

            # Build TTA context: prices + ledger state
            ctx = self._build_context(prices, ledger)

            # Evaluate TTA predicate (if set)
            if reg.tta is not None and not evaluate(reg.tta, ctx, self._prev_ctx):
                continue  # predicate did not fire, skip this bot

            try:
                intent = await reg.callback(bot_id, prices, ledger)
            except Exception:
                await logger.aexception("bot.decide.error", bot_id=bot_id)
                continue

            if intent is None or intent.action == IntentAction.HOLD:
                continue

            # 3. Route intent through coordinator
            try:
                result = await self.coordinator.execute(intent)
                if result.approved:
                    await logger.ainfo(
                        "trade.executed",
                        bot_id=bot_id,
                        symbol=intent.symbol,
                        action=intent.action,
                        quantity=str(intent.quantity),
                        order_id=result.order_result.order_id if result.order_result else None,
                    )
                    self._publish_event("trade.executed", {
                        "bot_id": bot_id,
                        "symbol": intent.symbol,
                        "action": str(intent.action),
                        "quantity": intent.quantity,
                        "rationale": intent.rationale,
                    })
                else:
                    reason = result.risk_decision.reason if result.risk_decision else "unknown"
                    await logger.ainfo(
                        "trade.rejected",
                        bot_id=bot_id,
                        symbol=intent.symbol,
                        reason=reason,
                    )
                    self._publish_event("trade.rejected", {
                        "bot_id": bot_id,
                        "symbol": intent.symbol,
                        "action": str(intent.action),
                        "quantity": intent.quantity,
                        "reason": reason,
                    })
            except Exception:
                await logger.aexception("trade.execute.error", bot_id=bot_id)

        # Save context for cross-predicate evaluation on next tick
        self._prev_ctx = self._build_context(prices, None)

        # Publish state snapshot
        self._publish_snapshot(tick_number, prices)

    def _build_context(
        self,
        prices: dict[str, Decimal],
        ledger: Ledger | None,
    ) -> Context:
        """Build a flat context dict for TTA evaluation."""
        ctx: Context = {}
        for symbol, price in prices.items():
            ctx[f"price.{symbol}"] = price

        if ledger is not None:
            ctx["ledger.equity"] = ledger.equity
            ctx["ledger.realized_pnl"] = ledger.realized_pnl
            ctx["ledger.drawdown_pct"] = ledger.drawdown_pct
            ctx["ledger.high_water_mark"] = ledger.high_water_mark
            ctx["ledger.position_count"] = Decimal(len(ledger.open_positions))

        return ctx

    def _publish_snapshot(
        self,
        tick: int,
        prices: dict[str, Decimal],
    ) -> None:
        """Publish state snapshot to DynamoDB (if publisher is configured)."""
        if self._publisher is None:
            return
        try:
            self._publisher.publish_snapshot(
                tick=tick,
                prices=prices,
                ledgers=self.coordinator.ledgers,
                risk_manager=self.coordinator.risk_manager,
            )
        except Exception:
            logger.exception("publisher.snapshot.error")

    def _publish_event(self, event_type: str, data: dict[str, object]) -> None:
        """Publish an event to DynamoDB (if publisher is configured)."""
        if self._publisher is None:
            return
        try:
            self._publisher.publish_event(event_type, data)
        except Exception:
            logger.exception("publisher.event.error")

    def stop(self) -> None:
        """Signal the tick loop to stop."""
        self._running = False


# Type alias for bot decision callbacks.
# Takes (bot_id, market_prices, ledger) and returns an optional TradeIntent.
BotCallback = Callable[
    [str, dict[str, Decimal], Ledger],
    Awaitable[TradeIntent | None],
]
