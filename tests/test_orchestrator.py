"""Tests for the orchestrator tick loop (§5.1)."""

from decimal import Decimal

import pytest

from trading_strands.broker.types import (
    AccountInfo,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from trading_strands.coordinator.coordinator import TradeCoordinator
from trading_strands.coordinator.types import IntentAction, TradeIntent
from trading_strands.ledger.models import FeeBreakdown, Ledger
from trading_strands.marketdata.provider import MarketDataProvider
from trading_strands.orchestrator.engine import Orchestrator
from trading_strands.risk.manager import RiskConfig, RiskManager


class StubBroker:
    def __init__(self) -> None:
        self.prices: dict[str, Decimal] = {"AAPL": Decimal("150.00")}
        self.order_count = 0

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        self.order_count += 1
        return OrderResult(
            order_id=f"orch-{self.order_count}",
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=self.prices.get(order.symbol, Decimal("100.00")),
            fees=FeeBreakdown(),
        )

    async def get_account(self) -> AccountInfo:
        return AccountInfo(
            cash=Decimal("50000"), portfolio_value=Decimal("50000"),
            buying_power=Decimal("50000"),
        )

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_quote(self, symbol: str) -> dict[str, object]:
        return {"price": self.prices.get(symbol, Decimal("100.00"))}

    def get_fee_schedule(self) -> FeeBreakdown:
        return FeeBreakdown()

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        return FeeBreakdown()


def _make_orchestrator(
    tick_interval: float = 0.0,
) -> tuple[Orchestrator, Ledger, StubBroker]:
    broker = StubBroker()
    risk_mgr = RiskManager(RiskConfig())
    ledger = Ledger(starting_capital=Decimal("50000"))
    coordinator = TradeCoordinator(
        broker=broker,
        risk_manager=risk_mgr,
        ledgers={"test-bot": ledger},
    )
    market_data = MarketDataProvider(broker)
    orch = Orchestrator(
        coordinator=coordinator,
        market_data=market_data,
        tick_interval=tick_interval,
    )
    return orch, ledger, broker


class TestOrchestrator:
    @pytest.mark.anyio
    async def test_bot_callback_invoked_each_tick(self) -> None:
        """Bot callback should be called on every tick."""
        orch, _ledger, _broker = _make_orchestrator()
        call_count = 0

        async def callback(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            nonlocal call_count
            call_count += 1
            return None  # hold

        orch.register_bot("test-bot", ["AAPL"], callback)
        await orch.run(max_ticks=5)
        assert call_count == 5

    @pytest.mark.anyio
    async def test_bot_trade_executed(self) -> None:
        """Bot returning a buy intent should result in a filled order."""
        orch, ledger, _broker = _make_orchestrator()

        async def buy_once(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            if len(ledger.open_positions) == 0:
                return TradeIntent(
                    bot_id=bot_id, symbol="AAPL",
                    action=IntentAction.BUY, quantity=Decimal("10"),
                )
            return None

        orch.register_bot("test-bot", ["AAPL"], buy_once)
        await orch.run(max_ticks=3)

        assert len(ledger.open_positions) == 1
        assert ledger.open_positions[0].symbol == "AAPL"
        assert ledger.open_positions[0].quantity == Decimal("10")

    @pytest.mark.anyio
    async def test_market_prices_passed_to_callback(self) -> None:
        """Bot callback should receive current market prices."""
        orch, _ledger, broker = _make_orchestrator()
        broker.prices["AAPL"] = Decimal("175.50")
        received_prices: list[dict[str, Decimal]] = []

        async def capture_prices(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            received_prices.append(dict(prices))
            return None

        orch.register_bot("test-bot", ["AAPL"], capture_prices)
        await orch.run(max_ticks=1)

        assert len(received_prices) == 1
        assert received_prices[0]["AAPL"] == Decimal("175.50")

    @pytest.mark.anyio
    async def test_stop_halts_loop(self) -> None:
        """Calling stop() should end the tick loop."""
        orch, _ledger, _broker = _make_orchestrator(tick_interval=0.01)
        ticks = 0

        async def count_and_stop(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            nonlocal ticks
            ticks += 1
            if ticks >= 3:
                orch.stop()
            return None

        orch.register_bot("test-bot", ["AAPL"], count_and_stop)
        await orch.run()
        assert ticks == 3

    @pytest.mark.anyio
    async def test_bot_error_does_not_crash_loop(self) -> None:
        """A bot raising an exception should not crash the orchestrator."""
        orch, _ledger, _broker = _make_orchestrator()
        call_count = 0

        async def flaky_bot(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                msg = "bot error"
                raise RuntimeError(msg)
            return None

        orch.register_bot("test-bot", ["AAPL"], flaky_bot)
        await orch.run(max_ticks=5)
        assert call_count == 5  # all 5 ticks executed despite error on tick 2

    @pytest.mark.anyio
    async def test_multiple_bots(self) -> None:
        """Multiple bots should each be called per tick."""
        orch, _ledger, _broker = _make_orchestrator()
        # Add a second bot ledger
        ledger2 = Ledger(starting_capital=Decimal("10000"))
        orch.coordinator.ledgers["bot-2"] = ledger2

        bot1_calls = 0
        bot2_calls = 0

        async def bot1(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            nonlocal bot1_calls
            bot1_calls += 1
            return None

        async def bot2(
            bot_id: str, prices: dict[str, Decimal], ledger: Ledger,
        ) -> TradeIntent | None:
            nonlocal bot2_calls
            bot2_calls += 1
            return None

        orch.register_bot("test-bot", ["AAPL"], bot1)
        orch.register_bot("bot-2", ["AAPL"], bot2)
        await orch.run(max_ticks=3)

        assert bot1_calls == 3
        assert bot2_calls == 3
