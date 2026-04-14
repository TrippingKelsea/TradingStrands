"""Full system integration test — orchestrator through ledger.

Simulates a multi-tick scenario where a bot follows a simple rule:
buy when price crosses above a threshold, sell when it drops below.
Verifies the entire pipeline wires correctly end-to-end.
"""

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


class ScriptedBroker:
    """Broker that plays back a scripted price sequence."""

    def __init__(self, price_script: list[Decimal]) -> None:
        self._prices = price_script
        self._tick = 0
        self.order_count = 0

    @property
    def current_price(self) -> Decimal:
        idx = min(self._tick, len(self._prices) - 1)
        return self._prices[idx]

    def advance_tick(self) -> None:
        self._tick += 1

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        self.order_count += 1
        return OrderResult(
            order_id=f"int-{self.order_count}",
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=self.current_price,
            fees=FeeBreakdown(
                commission=Decimal("0"),
                sec_fee=Decimal("0.02"),
                taf_fee=Decimal("0.01"),
            ),
        )

    async def get_account(self) -> AccountInfo:
        return AccountInfo(
            cash=Decimal("50000"), portfolio_value=Decimal("50000"),
            buying_power=Decimal("50000"),
        )

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_quote(self, symbol: str) -> dict[str, object]:
        return {"price": self.current_price}

    def get_fee_schedule(self) -> FeeBreakdown:
        return FeeBreakdown(sec_fee=Decimal("0.02"), taf_fee=Decimal("0.01"))

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        return FeeBreakdown(sec_fee=Decimal("0.02"), taf_fee=Decimal("0.01"))


class TestFullSystemIntegration:
    @pytest.mark.anyio
    async def test_breakout_strategy_lifecycle(self) -> None:
        """A bot that buys on breakout and sells on breakdown.

        Price script: 100, 102, 105 (buy!), 108, 103, 98 (sell!), 95
        Buy threshold: 104, Sell threshold: 100
        """
        prices = [
            Decimal("100"), Decimal("102"), Decimal("105"),
            Decimal("108"), Decimal("103"), Decimal("98"),
            Decimal("95"),
        ]
        broker = ScriptedBroker(prices)
        risk_mgr = RiskManager(RiskConfig())
        ledger = Ledger(starting_capital=Decimal("50000"))
        coordinator = TradeCoordinator(
            broker=broker, risk_manager=risk_mgr,
            ledgers={"breakout-bot": ledger},
        )
        market_data = MarketDataProvider(broker)
        orch = Orchestrator(
            coordinator=coordinator,
            market_data=market_data,
            tick_interval=0.0,
        )

        buy_threshold = Decimal("104")
        sell_threshold = Decimal("100")
        trade_log: list[str] = []

        async def breakout_strategy(
            bot_id: str, market_prices: dict[str, Decimal], bot_ledger: Ledger,
        ) -> TradeIntent | None:
            price = market_prices.get("AAPL", Decimal("0"))

            if not bot_ledger.open_positions and price > buy_threshold:
                trade_log.append(f"BUY@{price}")
                return TradeIntent(
                    bot_id=bot_id, symbol="AAPL",
                    action=IntentAction.BUY, quantity=Decimal("10"),
                    rationale=f"breakout above {buy_threshold}",
                )
            elif bot_ledger.open_positions and price < sell_threshold:
                trade_log.append(f"SELL@{price}")
                return TradeIntent(
                    bot_id=bot_id, symbol="AAPL",
                    action=IntentAction.SELL, quantity=Decimal("10"),
                    rationale=f"breakdown below {sell_threshold}",
                )
            return None

        orch.register_bot("breakout-bot", ["AAPL"], breakout_strategy)

        # Run all 7 ticks, advancing the broker's price each tick
        for _ in range(7):
            await orch._tick(0)
            broker.advance_tick()

        # Should have bought at $105 and sold at $98
        assert trade_log == ["BUY@105", "SELL@98"]

        # Position closed
        assert len(ledger.open_positions) == 0

        # PnL: bought 10 at $105 + $0.03 fees, sold 10 at $98 - $0.03 fees
        # Loss: ($98 - $105.003) * 10 = -$70.03, minus sell fees -$0.03 = -$70.06
        assert ledger.realized_pnl == Decimal("-70.06")

        # Two fills recorded
        assert len(ledger.order_history) == 2
        assert len(ledger.fee_ledger) == 2

        # Equity reflects the loss
        assert ledger.equity == Decimal("49929.94")

    @pytest.mark.anyio
    async def test_risk_limits_protect_during_drawdown(self) -> None:
        """After a big loss, risk manager prevents new positions."""
        prices = [
            Decimal("100"), Decimal("105"),  # buy
            Decimal("70"),  # sell (big loss)
            Decimal("110"),  # would buy, but drawdown exceeded
        ]
        broker = ScriptedBroker(prices)
        risk_mgr = RiskManager(RiskConfig(
            max_drawdown_pct=Decimal("0.05"),
            max_position_pct=Decimal("0.50"),
            max_total_exposure_pct=Decimal("0.90"),
        ))
        ledger = Ledger(starting_capital=Decimal("10000"))
        coordinator = TradeCoordinator(
            broker=broker, risk_manager=risk_mgr,
            ledgers={"risk-bot": ledger},
        )
        market_data = MarketDataProvider(broker)
        orch = Orchestrator(
            coordinator=coordinator, market_data=market_data,
            tick_interval=0.0,
        )

        trade_log: list[str] = []

        async def aggressive_bot(
            bot_id: str, market_prices: dict[str, Decimal], bot_ledger: Ledger,
        ) -> TradeIntent | None:
            price = market_prices.get("AAPL", Decimal("0"))
            if not bot_ledger.open_positions and price > Decimal("100"):
                trade_log.append(f"INTENT_BUY@{price}")
                return TradeIntent(
                    bot_id=bot_id, symbol="AAPL",
                    action=IntentAction.BUY, quantity=Decimal("30"),
                )
            elif bot_ledger.open_positions and price < Decimal("80"):
                trade_log.append(f"INTENT_SELL@{price}")
                return TradeIntent(
                    bot_id=bot_id, symbol="AAPL",
                    action=IntentAction.SELL, quantity=Decimal("30"),
                )
            return None

        orch.register_bot("risk-bot", ["AAPL"], aggressive_bot)

        for _ in range(4):
            await orch._tick(0)
            broker.advance_tick()

        # Bot tried to buy twice (at $105 and $110) and sell once (at $70)
        # First buy succeeded, sell succeeded, second buy should be blocked by drawdown
        assert "INTENT_BUY@105" in trade_log
        assert "INTENT_SELL@70" in trade_log
        assert "INTENT_BUY@110" in trade_log

        # But only 2 orders hit the broker (the second buy was risk-rejected)
        assert broker.order_count == 2

        # No open positions after the sell
        assert len(ledger.open_positions) == 0

        # Drawdown exceeds 5% limit
        assert ledger.drawdown_pct > Decimal("0.05")
