"""Orchestrator — tick loop that evaluates TTA predicates and wakes bots (§5.1)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

import anyio
import structlog

from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.ledger.models import Ledger
from trading_strands.marketdata.provider import MarketDataProvider

logger = structlog.get_logger()


class BotConfig:
    """Configuration for a single strategy bot."""

    def __init__(
        self,
        bot_id: str,
        symbols: list[str],
        starting_capital: Decimal,
    ) -> None:
        self.bot_id = bot_id
        self.symbols = symbols
        self.starting_capital = starting_capital


class Orchestrator:
    """Runs the tick loop, evaluates conditions, and dispatches to bots.

    For v0, the orchestrator is a simple loop that:
    1. Fetches market data for all watched symbols
    2. Calls each bot's decide() callback with the market snapshot
    3. Routes any resulting trade intents through the coordinator

    TTA predicate evaluation and IR materialization are deferred
    until the compilation agent is built.
    """

    def __init__(
        self,
        coordinator: TradeCoordinator,
        market_data: MarketDataProvider,
        tick_interval: float = 5.0,
    ) -> None:
        self.coordinator = coordinator
        self.market_data = market_data
        self.tick_interval = tick_interval
        self._bots: dict[str, BotCallback] = {}
        self._symbols: set[str] = set()
        self._running = False

    def register_bot(
        self,
        bot_id: str,
        symbols: list[str],
        callback: BotCallback,
    ) -> None:
        """Register a bot with its watched symbols and decision callback."""
        self._bots[bot_id] = callback
        self._symbols.update(symbols)

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
        # 1. Fetch market data
        prices = await self.market_data.get_prices(self._symbols)

        await logger.adebug("orchestrator.tick", tick=tick_number,
                            prices={s: str(p) for s, p in prices.items()})

        # 2. Wake each bot and collect intents
        for bot_id, callback in self._bots.items():
            ledger = self.coordinator.ledgers.get(bot_id)
            if ledger is None:
                continue

            try:
                intent = await callback(bot_id, prices, ledger)
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
                else:
                    await logger.ainfo(
                        "trade.rejected",
                        bot_id=bot_id,
                        symbol=intent.symbol,
                        reason=result.risk_decision.reason if result.risk_decision else "unknown",
                    )
            except Exception:
                await logger.aexception("trade.execute.error", bot_id=bot_id)

    def stop(self) -> None:
        """Signal the tick loop to stop."""
        self._running = False


# Type alias for bot decision callbacks.
# Takes (bot_id, market_prices, ledger) and returns an optional TradeIntent.
BotCallback = Callable[
    [str, dict[str, Decimal], Ledger],
    Awaitable[TradeIntent | None],
]
