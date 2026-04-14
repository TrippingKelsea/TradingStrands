"""End-to-end integration test: intent → coordinator → risk → broker → ledger.

Tests the full pipeline wiring without external dependencies. Uses a
stub broker (not a mock — a minimal protocol implementation) to verify
the coordinator correctly threads intent through risk evaluation,
order submission, and ledger recording.
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
from trading_strands.risk.manager import RiskConfig, RiskManager


class StubBroker:
    """Minimal broker for integration testing."""

    def __init__(self) -> None:
        self.fill_prices: dict[str, Decimal] = {
            "AAPL": Decimal("150.00"),
            "MSFT": Decimal("400.00"),
        }
        self.order_count = 0

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        self.order_count += 1
        price = self.fill_prices.get(order.symbol, Decimal("100.00"))
        return OrderResult(
            order_id=f"e2e-{self.order_count}",
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=price,
            fees=FeeBreakdown(
                commission=Decimal("0.00"),
                sec_fee=Decimal("0.02"),
                taf_fee=Decimal("0.01"),
                finra_fee=Decimal("0.01"),
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
        return {"price": self.fill_prices.get(symbol, Decimal("100.00"))}

    def get_fee_schedule(self) -> FeeBreakdown:
        return FeeBreakdown(sec_fee=Decimal("0.02"), taf_fee=Decimal("0.01"))

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        return FeeBreakdown(
            sec_fee=Decimal("0.02"), taf_fee=Decimal("0.01"),
            finra_fee=Decimal("0.01"),
        )


class TestEndToEnd:
    """Full pipeline integration tests."""

    def _make_system(
        self, capital: str = "50000",
    ) -> tuple[TradeCoordinator, Ledger, StubBroker]:
        broker = StubBroker()
        risk_mgr = RiskManager(RiskConfig(
            max_position_pct=Decimal("0.20"),
            max_total_exposure_pct=Decimal("0.80"),
            max_drawdown_pct=Decimal("0.15"),
            daily_loss_cap_pct=Decimal("0.05"),
        ))
        ledger = Ledger(starting_capital=Decimal(capital))
        coordinator = TradeCoordinator(
            broker=broker,
            risk_manager=risk_mgr,
            ledgers={"turtle-bot": ledger},
        )
        return coordinator, ledger, broker

    @pytest.mark.anyio
    async def test_full_round_trip(self) -> None:
        """Buy → hold → sell: full lifecycle of a position."""
        coord, ledger, _broker = self._make_system()

        # 1. Buy 10 AAPL at ~$150
        buy_result = await coord.execute(TradeIntent(
            bot_id="turtle-bot",
            symbol="AAPL",
            action=IntentAction.BUY,
            quantity=Decimal("10"),
            rationale="20-day breakout",
        ))
        assert buy_result.approved
        assert buy_result.order_result is not None
        assert buy_result.order_result.status == OrderStatus.FILLED
        assert len(ledger.open_positions) == 1
        assert ledger.open_positions[0].symbol == "AAPL"
        assert ledger.open_positions[0].quantity == Decimal("10")

        # Fees recorded
        assert len(ledger.fee_ledger) == 1
        assert ledger.fee_ledger[0].sec_fee == Decimal("0.02")

        # 2. Hold (no-op)
        hold_result = await coord.execute(TradeIntent(
            bot_id="turtle-bot",
            symbol="AAPL",
            action=IntentAction.HOLD,
            quantity=Decimal("0"),
            rationale="still above 10-day low",
        ))
        assert hold_result.approved
        assert hold_result.order_result is None
        assert len(ledger.open_positions) == 1  # unchanged

        # 3. Sell 10 AAPL at ~$150
        sell_result = await coord.execute(TradeIntent(
            bot_id="turtle-bot",
            symbol="AAPL",
            action=IntentAction.SELL,
            quantity=Decimal("10"),
            rationale="broke below 10-day low",
        ))
        assert sell_result.approved
        assert len(ledger.open_positions) == 0

        # PnL: bought at $150 + $0.04 fees, sold at $150 - $0.04 fees
        # = -$0.08 total fees, break even on price
        assert ledger.realized_pnl == Decimal("-0.08")
        assert len(ledger.fee_ledger) == 2
        assert len(ledger.order_history) == 2

    @pytest.mark.anyio
    async def test_winning_trade_clears_fees(self) -> None:
        """A trade is only profitable if it clears its fees."""
        coord, ledger, broker = self._make_system()

        # Buy at $150
        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("10"),
        ))

        # Price goes to $160
        broker.fill_prices["AAPL"] = Decimal("160.00")

        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.SELL, quantity=Decimal("10"),
        ))

        # Gross profit: ($160 - $150) * 10 = $100
        # Total fees: $0.04 * 2 = $0.08
        # Net: $99.92
        assert ledger.realized_pnl == Decimal("99.92")
        assert ledger.equity == Decimal("50099.92")
        assert ledger.high_water_mark == Decimal("50099.92")

    @pytest.mark.anyio
    async def test_losing_trade_includes_fees(self) -> None:
        """Losses include fees — fully burdened."""
        coord, ledger, broker = self._make_system()

        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("10"),
        ))

        broker.fill_prices["AAPL"] = Decimal("140.00")

        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.SELL, quantity=Decimal("10"),
        ))

        # Gross loss: ($140 - $150) * 10 = -$100
        # Fees: $0.08
        # Net: -$100.08
        assert ledger.realized_pnl == Decimal("-100.08")

    @pytest.mark.anyio
    async def test_risk_blocks_oversized_position(self) -> None:
        """Risk manager blocks a position that exceeds per-position limit."""
        coord, ledger, _ = self._make_system()

        # Try to buy $15000 worth (30% of $50000 equity, limit is 20%)
        result = await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("100"),
        ))

        assert not result.approved
        assert result.order_result is None
        assert len(ledger.open_positions) == 0

    @pytest.mark.anyio
    async def test_multi_position_exposure_limit(self) -> None:
        """Risk manager blocks when total exposure exceeds limit."""
        coord, _ledger, _ = self._make_system()

        # Buy $9000 AAPL (18% of equity, under 20% per-position)
        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("60"),
        ))

        # Buy $8000 MSFT (16% of equity, under 20% per-position)
        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="MSFT",
            action=IntentAction.BUY, quantity=Decimal("20"),
        ))

        # Now at $17000 / $50000 = 34% exposure. Try to buy $9000 more AAPL.
        # Would be $26000 / $50000 = 52%, still under 80%... let's push harder.
        # Actually let's buy enough to breach 80%
        result = await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("200"),
        ))

        # 200 * $150 = $30000 + existing $17000 = $47000 / $50000 = 94% > 80%
        assert not result.approved

    @pytest.mark.anyio
    async def test_fee_ledger_accumulates_across_trades(self) -> None:
        """Fee ledger should contain all fee breakdowns from all fills."""
        coord, ledger, _broker = self._make_system()

        for _ in range(3):
            await coord.execute(TradeIntent(
                bot_id="turtle-bot", symbol="AAPL",
                action=IntentAction.BUY, quantity=Decimal("5"),
            ))

        assert len(ledger.fee_ledger) == 3
        total_fees = sum(f.total for f in ledger.fee_ledger)
        # 3 * $0.04 = $0.12
        assert total_fees == Decimal("0.12")

    @pytest.mark.anyio
    async def test_drawdown_halts_new_buys(self) -> None:
        """After a large loss causing drawdown > limit, new buys are blocked."""
        coord, ledger, broker = self._make_system("10000")
        # Relax per-position limit so we can open a big position
        coord.risk_manager._config.max_position_pct = Decimal("0.80")
        coord.risk_manager._config.max_total_exposure_pct = Decimal("0.90")

        # Buy and sell at a big loss to trigger drawdown
        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("50"),
        ))
        assert len(ledger.open_positions) == 1

        broker.fill_prices["AAPL"] = Decimal("120.00")
        await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="AAPL",
            action=IntentAction.SELL, quantity=Decimal("50"),
        ))
        # Lost ($150-$120)*50 = $1500 + fees, equity ~$8500, HWM=$10000
        # Drawdown ~15%
        assert len(ledger.open_positions) == 0
        assert ledger.drawdown_pct > Decimal("0.15")

        # New buy should be blocked
        broker.fill_prices["MSFT"] = Decimal("50.00")
        result = await coord.execute(TradeIntent(
            bot_id="turtle-bot", symbol="MSFT",
            action=IntentAction.BUY, quantity=Decimal("5"),
        ))
        assert not result.approved
        assert "drawdown" in result.risk_decision.reason.lower()
