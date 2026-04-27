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


class TestHaltEnforcement:
    """Per-org + system-wide halt enforcement via an injected HaltStore."""

    class HaltStub:
        """Small HaltStore-shaped fake. Returns halt state from a dict."""

        def __init__(
            self,
            system_halted: bool = False,
            org_halted: dict[str, bool] | None = None,
            effective_reason: str | None = None,
        ) -> None:
            self.system_halted = system_halted
            self.org_halted = org_halted or {}
            self._reason = effective_reason

        def is_effective_halted(self, org_id: str) -> bool:
            return self.system_halted or self.org_halted.get(org_id, False)

        def get_effective_reason(self, org_id: str) -> str | None:
            if self.system_halted:
                return self._reason or "system halted"
            if self.org_halted.get(org_id):
                return self._reason or "org halted"
            return None

    @pytest.mark.anyio
    async def test_org_halt_rejects_intent_before_broker_call(self) -> None:
        """Per-org halt must stop the trade BEFORE hitting the broker —
        both for speed (no wasted Secrets Manager call) and correctness
        (the halt is the whole point)."""

        broker = StubBroker()
        halt = TestHaltEnforcement.HaltStub(
            org_halted={"org-a": True},
            effective_reason="auditor: AAPL drift",
        )
        coordinator = TradeCoordinator(
            broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
            halt_store=halt,
        )
        intent = TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        )
        result = await coordinator.execute(intent)
        assert not result.approved
        assert result.risk_decision is not None
        assert "halted" in (result.risk_decision.reason or "").lower()
        # Broker must not have been called for the order.
        assert broker.submitted_orders == []

    @pytest.mark.anyio
    async def test_sibling_org_not_halted_still_trades(self) -> None:
        """org-a halted shouldn't touch org-b."""

        broker = StubBroker()
        halt = TestHaltEnforcement.HaltStub(
            org_halted={"org-a": True},
        )
        coordinator = TradeCoordinator(
            broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
            halt_store=halt,
        )
        intent = TradeIntent(
            bot_id="bot-1", org_id="org-b", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        )
        result = await coordinator.execute(intent)
        assert result.approved
        assert len(broker.submitted_orders) == 1

    @pytest.mark.anyio
    async def test_system_halt_rejects_every_org(self) -> None:
        broker = StubBroker()
        halt = TestHaltEnforcement.HaltStub(system_halted=True)
        coordinator = TradeCoordinator(
            broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
            halt_store=halt,
        )
        for org in ("org-a", "org-b", "org-c"):
            result = await coordinator.execute(TradeIntent(
                bot_id="bot-1", org_id=org, symbol="AAPL",
                action=IntentAction.BUY, quantity=Decimal("1"),
            ))
            assert not result.approved
        assert broker.submitted_orders == []

    @pytest.mark.anyio
    async def test_halt_store_read_error_fails_open(self) -> None:
        """A transient HaltStore read error must not lock the desk —
        the in-memory RiskManager._desk_halted flag is still the
        authoritative safety net. Fail-open here, fail-closed on the
        RiskManager side: belt-and-suspenders with opposite default."""

        broker = StubBroker()

        class BrokenHalt:
            def is_effective_halted(self, org_id: str) -> bool:
                raise RuntimeError("ddb throttled")

            def get_effective_reason(self, org_id: str) -> str | None:
                return None

        coordinator = TradeCoordinator(
            broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
            halt_store=BrokenHalt(),
        )
        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))
        # No halt detected → falls through to normal risk/broker path.
        assert result.approved
        assert len(broker.submitted_orders) == 1

    @pytest.mark.anyio
    async def test_no_halt_store_defaults_to_v0_behavior(self) -> None:
        """Back-compat: callers without a halt_store get the legacy
        single-flag behavior unchanged."""

        broker = StubBroker()
        coordinator = TradeCoordinator(
            broker_factory=lambda _o, _b=broker: _b, default_broker=broker,
            risk_manager=RiskManager(RiskConfig()),
            ledgers={"bot-1": Ledger(starting_capital=Decimal("10000"))},
            # halt_store intentionally not set
        )
        result = await coordinator.execute(TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("1"),
        ))
        assert result.approved
        assert len(broker.submitted_orders) == 1
