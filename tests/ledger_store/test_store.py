"""Tests for the durable ledger store.

Coverage focuses on the invariant that matters: a bot can restart and
resume trading with the same state it had before. Data-model faithfulness
(Decimals, fills, PnL math) is verified by test_ledger.py — here we only
test the persistence layer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading_strands.ledger.models import FeeBreakdown, Fill, Ledger, Side
from trading_strands.ledger_store.store import LedgerStore


def _fill(
    symbol: str = "AAPL", side: Side = Side.BUY,
    qty: str = "10", price: str = "150.00",
) -> Fill:
    return Fill(
        symbol=symbol, side=side,
        quantity=Decimal(qty), price=Decimal(price),
        fees=FeeBreakdown(commission=Decimal("1.00")),
    )


def test_snapshot_roundtrip_preserves_pnl_and_positions(table: Any) -> None:
    store = LedgerStore(table)
    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(_fill())  # buy 10 AAPL @ 150

    store.save_snapshot("bot-1", ledger)
    loaded = store.load_snapshot("bot-1")

    assert loaded is not None
    assert loaded.starting_capital == Decimal("10000")
    assert len(loaded.open_positions) == 1
    assert loaded.open_positions[0].symbol == "AAPL"
    assert loaded.open_positions[0].quantity == Decimal("10")


def test_snapshot_preserves_realized_pnl(table: Any) -> None:
    """After buy+sell, realized PnL persists across load/save."""

    store = LedgerStore(table)
    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(_fill(qty="10", price="150"))
    ledger.record_fill(_fill(side=Side.SELL, qty="10", price="160"))

    store.save_snapshot("bot-1", ledger)
    loaded = store.load_snapshot("bot-1")

    assert loaded is not None
    # Realized PnL should be positive (bought at 150, sold at 160, minus fees).
    assert loaded.realized_pnl > Decimal("0")
    # Position fully closed.
    assert loaded.open_positions == []


def test_load_snapshot_missing_returns_none(table: Any) -> None:
    store = LedgerStore(table)
    assert store.load_snapshot("never-existed") is None


def test_snapshot_overwritten_not_appended(table: Any) -> None:
    """save_snapshot always overwrites; only the latest state is in DDB."""

    store = LedgerStore(table)
    ledger = Ledger(starting_capital=Decimal("10000"))

    store.save_snapshot("bot-1", ledger)
    ledger.record_fill(_fill())
    store.save_snapshot("bot-1", ledger)
    ledger.record_fill(_fill(symbol="MSFT", qty="5", price="300"))
    store.save_snapshot("bot-1", ledger)

    loaded = store.load_snapshot("bot-1")
    assert loaded is not None
    # The final state should have two positions, not three saved versions.
    assert len(loaded.open_positions) == 2


def test_append_event_stores_ttl(table: Any) -> None:
    """Each event carries a ttl attribute for DDB-managed cleanup."""

    store = LedgerStore(table, event_retention_days=7)
    store.append_event("bot-1", _fill())

    resp = table.scan(
        FilterExpression="begins_with(pk, :p)",
        ExpressionAttributeValues={":p": "LEDGER_EVENT#bot-1#"},
    )
    items = resp.get("Items", [])
    assert len(items) == 1
    assert "ttl" in items[0]
    assert "fill_json" in items[0]
    assert items[0]["event_type"] == "fill"


def test_events_for_bot_ordered_newest_first(table: Any) -> None:
    """Events written across distinct seconds should be newest-first.

    Uses sleep(1.01) so the `ts` attribute (unix seconds) differs per call —
    the store sorts by ts first. Sub-second ordering is deterministic via
    the random pk suffix tiebreaker but not semantically meaningful to
    callers; we only guarantee second-resolution ordering."""

    import time
    store = LedgerStore(table)
    store.append_event("bot-1", _fill(symbol="AAPL"))
    time.sleep(1.01)
    store.append_event("bot-1", _fill(symbol="MSFT"))
    time.sleep(1.01)
    store.append_event("bot-1", _fill(symbol="GOOG"))

    events = store.events_for_bot("bot-1")
    assert len(events) == 3
    # Newest first.
    assert "GOOG" in events[0]["fill_json"]
    assert "AAPL" in events[2]["fill_json"]


def test_events_isolated_per_bot(table: Any) -> None:
    """bot-1's events must not appear in bot-2's query."""

    store = LedgerStore(table)
    store.append_event("bot-1", _fill(symbol="AAPL"))
    store.append_event("bot-2", _fill(symbol="MSFT"))

    b1 = store.events_for_bot("bot-1")
    b2 = store.events_for_bot("bot-2")
    assert len(b1) == 1 and "AAPL" in b1[0]["fill_json"]
    assert len(b2) == 1 and "MSFT" in b2[0]["fill_json"]


def test_record_and_persist_end_to_end(table: Any) -> None:
    """The combined helper updates in-memory state, logs the event, and
    saves the snapshot in one call."""

    store = LedgerStore(table)
    ledger = Ledger(starting_capital=Decimal("10000"))
    fill = _fill()

    store.record_and_persist("bot-1", ledger, fill)

    # In-memory state updated.
    assert len(ledger.open_positions) == 1
    # Snapshot persisted.
    loaded = store.load_snapshot("bot-1")
    assert loaded is not None
    assert len(loaded.open_positions) == 1
    # Event logged.
    events = store.events_for_bot("bot-1")
    assert len(events) == 1


def test_restart_recovery_scenario(table: Any) -> None:
    """The invariant: a restarted bot can resume with full state.

    This is the scenario that motivated the whole module — the scale-down
    scheduler stops the trading service nightly and restarts at market
    open. Without this, Tuesday's bot wakes up with realized_pnl=0.
    """

    store = LedgerStore(table)

    # Day 1: bot runs, makes some trades, accumulates PnL.
    day1_ledger = Ledger(starting_capital=Decimal("10000"))
    store.record_and_persist("bot-1", day1_ledger,
                              _fill(qty="10", price="150"))
    store.record_and_persist("bot-1", day1_ledger,
                              _fill(side=Side.SELL, qty="10", price="160"))

    prior_pnl = day1_ledger.realized_pnl
    prior_hwm = day1_ledger.high_water_mark
    assert prior_pnl > 0

    # Simulate task stop. "Day 2" wakes up and tries to reload.
    recovered = store.load_snapshot("bot-1")
    assert recovered is not None
    assert recovered.realized_pnl == prior_pnl
    assert recovered.high_water_mark == prior_hwm
    assert recovered.starting_capital == Decimal("10000")
    assert recovered.open_positions == []  # prior sell closed the position


def test_coordinator_persists_fills_via_store(table: Any) -> None:
    """Integration: TradeCoordinator with ledger_store wired persists
    every fill end-to-end. Loading the snapshot back reproduces the
    post-trade state."""

    import anyio

    from trading_strands.broker.types import (
        AccountInfo,
        BrokerPosition,
        OrderRequest,
        OrderResult,
        OrderStatus,
    )
    from trading_strands.coordinator.coordinator import TradeCoordinator
    from trading_strands.coordinator.types import IntentAction, TradeIntent
    from trading_strands.risk.manager import RiskConfig, RiskManager

    class StubBroker:
        async def submit_order(self, order: OrderRequest) -> OrderResult:
            return OrderResult(
                order_id="x", status=OrderStatus.FILLED,
                filled_quantity=order.quantity,
                filled_price=Decimal("150"),
                fees=FeeBreakdown(commission=Decimal("1")),
            )

        async def get_account(self) -> AccountInfo:
            return AccountInfo(
                cash=Decimal("10000"), portfolio_value=Decimal("10000"),
                buying_power=Decimal("10000"),
            )

        async def get_positions(self) -> list[BrokerPosition]:
            return []

        async def get_quote(self, symbol: str) -> dict[str, object]:
            return {"price": Decimal("150")}

        def get_fee_schedule(self) -> FeeBreakdown:
            return FeeBreakdown()

        def estimate_fees(self, order: OrderRequest) -> FeeBreakdown:
            return FeeBreakdown()

    store = LedgerStore(table)
    broker = StubBroker()
    ledger = Ledger(starting_capital=Decimal("10000"))
    coord = TradeCoordinator(
        broker_factory=lambda _org, _b=broker: _b,
        risk_manager=RiskManager(RiskConfig()),
        ledgers={"bot-1": ledger},
        default_broker=broker,
        ledger_store=store,
    )

    async def execute() -> None:
        await coord.execute(TradeIntent(
            bot_id="bot-1", org_id="org-a", symbol="AAPL",
            action=IntentAction.BUY, quantity=Decimal("10"),
        ))

    anyio.run(execute)

    loaded = store.load_snapshot("bot-1")
    assert loaded is not None
    assert len(loaded.open_positions) == 1
    assert loaded.open_positions[0].symbol == "AAPL"
    # Event appended too.
    events = store.events_for_bot("bot-1")
    assert len(events) == 1


def test_snapshot_carries_fee_history(table: Any) -> None:
    """Fee ledger survives the round trip — the auditor relies on this."""

    store = LedgerStore(table)
    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.record_fill(_fill())
    ledger.record_fill(_fill(symbol="MSFT", qty="5", price="300"))
    store.save_snapshot("bot-1", ledger)

    loaded = store.load_snapshot("bot-1")
    assert loaded is not None
    assert len(loaded.fee_ledger) == 2
    total_fees = sum((f.commission for f in loaded.fee_ledger), Decimal("0"))
    assert total_fees == Decimal("2.00")
