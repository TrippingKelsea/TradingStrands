"""Tests for kill-switch verb implementations (§6.1)."""

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
from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.risk.killswitch import KillSwitch, KillSwitchVerb
from trading_strands.risk.manager import RiskConfig, RiskManager


class StubBroker:
    def __init__(self) -> None:
        self.submitted_orders: list[OrderRequest] = []
        self.order_count = 0

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submitted_orders.append(order)
        self.order_count += 1
        return OrderResult(
            order_id=f"ks-{self.order_count}",
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=Decimal("100.00"),
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
        return {"price": Decimal("100.00")}

    def get_fee_schedule(self) -> FeeBreakdown:
        return FeeBreakdown()

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        return FeeBreakdown()


def _make_system() -> tuple[KillSwitch, TradeCoordinator, Ledger, StubBroker]:
    broker = StubBroker()
    risk_mgr = RiskManager(RiskConfig())
    ledger = Ledger(starting_capital=Decimal("50000"))
    coordinator = TradeCoordinator(
        broker=broker, risk_manager=risk_mgr,
        ledgers={"bot-1": ledger},
    )
    ks = KillSwitch(coordinator=coordinator, risk_manager=risk_mgr)
    return ks, coordinator, ledger, broker


class TestHaltAndStopTrading:
    @pytest.mark.anyio
    async def test_halts_bot(self) -> None:
        ks, coordinator, _ledger, _broker = _make_system()
        await ks.execute(KillSwitchVerb.HALT_AND_STOP, bot_id="bot-1")

        # Bot should be halted — new trades rejected
        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("10"),
        ))
        assert not result.approved

    @pytest.mark.anyio
    async def test_halts_desk(self) -> None:
        ks, coordinator, _ledger, _broker = _make_system()
        await ks.execute(KillSwitchVerb.HALT_AND_STOP)

        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("10"),
        ))
        assert not result.approved

    @pytest.mark.anyio
    async def test_positions_held_as_is(self) -> None:
        """halt-and-stop should NOT close existing positions."""
        ks, _coordinator, ledger, broker = _make_system()
        # Open a position first
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))

        await ks.execute(KillSwitchVerb.HALT_AND_STOP, bot_id="bot-1")

        # Position should still be there
        assert len(ledger.open_positions) == 1
        # No sell orders submitted to broker
        assert len(broker.submitted_orders) == 0


class TestHaltAndLiquidate:
    @pytest.mark.anyio
    async def test_liquidates_all_positions(self) -> None:
        """halt-and-liquidate should sell everything at market."""
        ks, _coordinator, ledger, broker = _make_system()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("5"),
            price=Decimal("200.00"), fees=FeeBreakdown(),
        ))

        await ks.execute(KillSwitchVerb.HALT_AND_LIQUIDATE, bot_id="bot-1")

        # Both positions should be closed
        assert len(ledger.open_positions) == 0
        # Two sell orders submitted
        assert len(broker.submitted_orders) == 2
        sell_symbols = {o.symbol for o in broker.submitted_orders}
        assert sell_symbols == {"AAPL", "MSFT"}

    @pytest.mark.anyio
    async def test_bot_halted_after_liquidation(self) -> None:
        ks, coordinator, ledger, _broker = _make_system()
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("100.00"), fees=FeeBreakdown(),
        ))

        await ks.execute(KillSwitchVerb.HALT_AND_LIQUIDATE, bot_id="bot-1")

        # New trades should be rejected
        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("5"),
        ))
        assert not result.approved


class TestHaltAndSellGains:
    @pytest.mark.anyio
    async def test_sells_only_winning_positions(self) -> None:
        """halt-and-sell-gains closes winners, holds losers."""
        ks, _coordinator, ledger, broker = _make_system()
        # AAPL: bought at $100, now at $100 (use broker quote)
        ledger.record_fill(Fill(
            symbol="AAPL", side=Side.BUY, quantity=Decimal("10"),
            price=Decimal("90.00"), fees=FeeBreakdown(),
        ))
        # MSFT: bought at $200, now at $100 (losing)
        ledger.record_fill(Fill(
            symbol="MSFT", side=Side.BUY, quantity=Decimal("5"),
            price=Decimal("200.00"), fees=FeeBreakdown(),
        ))

        market_prices = {"AAPL": Decimal("110.00"), "MSFT": Decimal("80.00")}
        await ks.execute(
            KillSwitchVerb.HALT_AND_SELL_GAINS,
            bot_id="bot-1",
            market_prices=market_prices,
        )

        # Only AAPL (winner) should be sold
        assert len(broker.submitted_orders) == 1
        assert broker.submitted_orders[0].symbol == "AAPL"

        # MSFT (loser) should still be open
        remaining = [p.symbol for p in ledger.open_positions]
        assert "MSFT" in remaining


class TestKillSwitchLog:
    @pytest.mark.anyio
    async def test_logs_events(self) -> None:
        ks, _coordinator, _ledger, _broker = _make_system()
        await ks.execute(KillSwitchVerb.HALT_AND_STOP, bot_id="bot-1")

        assert len(ks.log) == 1
        assert ks.log[0]["verb"] == KillSwitchVerb.HALT_AND_STOP
        assert ks.log[0]["bot_id"] == "bot-1"
