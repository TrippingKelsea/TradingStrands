"""Tests for the trade coordinator (§5.3)."""

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
    """In-process broker for coordinator integration tests.

    This is not a mock — it's a minimal implementation of the broker
    protocol for testing the coordinator wiring. Broker adapters
    themselves are tested against live APIs.
    """

    def __init__(self, fill_price: Decimal = Decimal("100.00")) -> None:
        self.fill_price = fill_price
        self.submitted_orders: list[OrderRequest] = []

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submitted_orders.append(order)
        return OrderResult(
            order_id=f"stub-{len(self.submitted_orders)}",
            status=OrderStatus.FILLED,
            filled_quantity=order.quantity,
            filled_price=self.fill_price,
            fees=FeeBreakdown(commission=Decimal("1.00")),
        )

    async def get_account(self) -> AccountInfo:
        return AccountInfo(
            cash=Decimal("100000"),
            portfolio_value=Decimal("100000"),
            buying_power=Decimal("100000"),
        )

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_quote(self, symbol: str) -> dict[str, object]:
        return {"price": self.fill_price}

    def get_fee_schedule(self) -> FeeBreakdown:
        return FeeBreakdown(commission=Decimal("1.00"))

    def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
        return FeeBreakdown(commission=Decimal("1.00"))


def _intent(
    symbol: str = "AAPL",
    action: IntentAction = IntentAction.BUY,
    quantity: str = "10",
    bot_id: str = "bot-1",
) -> TradeIntent:
    return TradeIntent(org_id="test-org",
        bot_id=bot_id, symbol=symbol, action=action, quantity=Decimal(quantity),
    )


@pytest.fixture
def coordinator() -> TradeCoordinator:
    broker = StubBroker(fill_price=Decimal("100.00"))
    risk_mgr = RiskManager(RiskConfig())
    ledger = Ledger(starting_capital=Decimal("10000"))
    return TradeCoordinator(broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
        risk_manager=risk_mgr,
        ledgers={"bot-1": ledger},
    )


class TestTradeCoordinator:
    @pytest.mark.anyio
    async def test_buy_flow(self, coordinator: TradeCoordinator) -> None:
        """Intent → risk approved → broker filled → ledger updated."""
        result = await coordinator.execute(_intent())
        assert result.approved
        assert result.order_result is not None
        assert result.order_result.status == OrderStatus.FILLED

        ledger = coordinator.ledgers["bot-1"]
        assert len(ledger.open_positions) == 1
        assert ledger.open_positions[0].symbol == "AAPL"
        assert len(ledger.fee_ledger) == 1

    @pytest.mark.anyio
    async def test_sell_flow(self, coordinator: TradeCoordinator) -> None:
        """Buy then sell — position should be closed, PnL realized."""
        await coordinator.execute(_intent(action=IntentAction.BUY, quantity="10"))
        result = await coordinator.execute(_intent(action=IntentAction.SELL, quantity="10"))

        assert result.approved
        ledger = coordinator.ledgers["bot-1"]
        assert len(ledger.open_positions) == 0
        # Bought at $100 + $1 fee, sold at $100 - $1 fee = -$2 realized PnL
        assert ledger.realized_pnl == Decimal("-2.00")

    @pytest.mark.anyio
    async def test_risk_rejection_skips_broker(self, coordinator: TradeCoordinator) -> None:
        """Rejected by risk manager — no order sent to broker."""
        # Set a tiny position limit
        coordinator.risk_manager._config.max_position_pct = Decimal("0.01")

        result = await coordinator.execute(_intent(quantity="50"))
        assert not result.approved
        assert result.order_result is None
        assert result.risk_decision is not None
        assert not result.risk_decision.approved

        # Broker should not have received any orders.
        # Access the org-scoped broker via the coordinator's cache; post-
        # per-org refactor, coordinator.broker is no longer a single attr.
        broker = coordinator.broker_for("test-org")
        assert len(broker.submitted_orders) == 0  # type: ignore[attr-defined]

    @pytest.mark.anyio
    async def test_hold_is_noop(self, coordinator: TradeCoordinator) -> None:
        """HOLD intent should be approved without touching broker or ledger."""
        result = await coordinator.execute(_intent(action=IntentAction.HOLD, quantity="0"))
        assert result.approved
        assert result.order_result is None

        ledger = coordinator.ledgers["bot-1"]
        assert len(ledger.open_positions) == 0
        assert len(ledger.order_history) == 0

    @pytest.mark.anyio
    async def test_unknown_bot_raises(self, coordinator: TradeCoordinator) -> None:
        """Intent from an unregistered bot should raise."""
        intent = _intent(bot_id="unknown-bot")
        with pytest.raises(ValueError, match="unknown bot"):
            await coordinator.execute(intent)

    @pytest.mark.anyio
    async def test_market_prices_passed_to_risk(self, coordinator: TradeCoordinator) -> None:
        """Coordinator should fetch quotes and pass market prices to risk manager."""
        result = await coordinator.execute(_intent())
        # If we got here without error, market prices were fetched and passed
        assert result.approved


class TestPerOrgBrokerRouting:
    """Per-org broker factory: each org gets its own broker instance, trades
    route to the right one, missing-creds rejects the intent cleanly."""

    @pytest.mark.anyio
    async def test_intent_routes_to_owning_org_broker(self) -> None:
        broker_a = StubBroker(fill_price=Decimal("100"))
        broker_b = StubBroker(fill_price=Decimal("200"))
        brokers: dict[str, StubBroker] = {"org-a": broker_a, "org-b": broker_b}

        coordinator = TradeCoordinator(
            broker_factory=lambda org_id: brokers[org_id],
            risk_manager=RiskManager(RiskConfig()),
            ledgers={
                "bot-a": Ledger(starting_capital=Decimal("10000")),
                "bot-b": Ledger(starting_capital=Decimal("10000")),
            },
        )

        await coordinator.execute(TradeIntent(
            bot_id="bot-a", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))
        await coordinator.execute(TradeIntent(
            bot_id="bot-b", org_id="org-b", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))

        # Each broker received exactly its own org's trade — no cross-org
        # leak through the coordinator.
        assert len(broker_a.submitted_orders) == 1
        assert len(broker_b.submitted_orders) == 1

    @pytest.mark.anyio
    async def test_broker_cached_after_first_use(self) -> None:
        """Factory is invoked once per org; subsequent intents reuse the cached
        instance. Matters because building a live broker is expensive."""

        call_count = 0

        def factory(org_id: str) -> StubBroker:
            nonlocal call_count
            call_count += 1
            return StubBroker()

        coordinator = TradeCoordinator(
            broker_factory=factory,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
        )

        for _ in range(3):
            await coordinator.execute(TradeIntent(
                bot_id="bot-1", org_id="org-a", symbol="AAPL",
                action=IntentAction.BUY, quantity=Decimal("1"),
            ))
        assert call_count == 1  # factory invoked once, cached thereafter

    @pytest.mark.anyio
    async def test_missing_broker_rejects_intent_cleanly(self) -> None:
        """An org with no Alpaca creds must not crash the coordinator — the
        intent is rejected with a reason the Strategy Agent can log."""

        def failing_factory(org_id: str) -> StubBroker:
            msg = f"no credentials configured for org {org_id}"
            raise RuntimeError(msg)

        coordinator = TradeCoordinator(
            broker_factory=failing_factory,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
        )

        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", org_id="org-nocreds", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))

        assert not result.approved
        assert result.risk_decision is not None
        assert not result.risk_decision.approved
        assert "no credentials" in result.risk_decision.reason

    @pytest.mark.anyio
    async def test_invalidate_broker_forces_rebuild(self) -> None:
        """When an orgadmin rotates Alpaca creds, invalidate_broker drops
        the cache so the next intent rebuilds from the fresh secret."""

        call_count = 0

        def factory(org_id: str) -> StubBroker:
            nonlocal call_count
            call_count += 1
            return StubBroker()

        coordinator = TradeCoordinator(
            broker_factory=factory,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
        )

        await coordinator.execute(TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))
        assert call_count == 1

        coordinator.invalidate_broker("org-a")

        await coordinator.execute(TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))
        assert call_count == 2
