"""Tests for the DynamoDB state publisher."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from trading_strands.dashboard.publisher import StatePublisher
from trading_strands.ledger.models import Ledger, Position
from trading_strands.risk.manager import RiskConfig, RiskManager


def _make_publisher() -> tuple[StatePublisher, MagicMock]:
    """Create a publisher with a mocked DynamoDB table."""
    mock_table = MagicMock()
    with patch("trading_strands.dashboard.publisher.boto3") as mock_boto3:
        mock_boto3.resource.return_value.Table.return_value = mock_table
        publisher = StatePublisher("test-table")
    return publisher, mock_table


def test_publish_snapshot_basic() -> None:
    publisher, mock_table = _make_publisher()

    ledger = Ledger(starting_capital=Decimal("10000"))
    ledger.open_positions.append(
        Position(symbol="AAPL", quantity=Decimal("10"), burdened_cost_basis=Decimal("150"))
    )

    risk = RiskManager(RiskConfig())
    prices = {"AAPL": Decimal("155.50")}

    publisher.publish_snapshot(
        tick=42,
        prices=prices,
        ledgers={"bot-0": ledger},
        risk_manager=risk,
    )

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]

    assert item["pk"] == "SNAPSHOT"
    assert item["tick"] == 42
    assert "timestamp" in item
    assert item["prices"]["AAPL"] == "155.50"
    assert item["ledgers"]["bot-0"]["equity"] == "10000"
    assert len(item["ledgers"]["bot-0"]["positions"]) == 1
    assert item["ledgers"]["bot-0"]["positions"][0]["symbol"] == "AAPL"
    assert item["risk"]["desk_halted"] is False
    assert item["risk"]["halted_bots"] == []


def test_publish_snapshot_with_whatif() -> None:
    publisher, mock_table = _make_publisher()

    risk = RiskManager(RiskConfig())
    whatif = {
        "total_entries": 3,
        "total_unrealized_pnl": Decimal("42.50"),
        "best_missed": {"symbol": "AAPL", "pnl": Decimal("100")},
    }

    publisher.publish_snapshot(
        tick=1,
        prices={},
        ledgers={},
        risk_manager=risk,
        whatif_summary=whatif,
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["whatif"]["total_entries"] == 3
    assert item["whatif"]["total_unrealized_pnl"] == "42.50"


def test_publish_snapshot_halted_desk() -> None:
    publisher, mock_table = _make_publisher()

    risk = RiskManager(RiskConfig())
    risk.halt_desk()
    risk.halt_bot("bot-1")

    publisher.publish_snapshot(
        tick=0,
        prices={},
        ledgers={},
        risk_manager=risk,
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["risk"]["desk_halted"] is True
    assert "bot-1" in item["risk"]["halted_bots"]


def test_publish_event() -> None:
    publisher, mock_table = _make_publisher()

    publisher.publish_event("trade.executed", {
        "bot_id": "bot-0",
        "symbol": "AAPL",
        "action": "buy",
        "quantity": Decimal("10"),
    })

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]

    assert item["pk"].startswith("EVENT#")
    assert item["event_type"] == "trade.executed"
    assert item["data"]["symbol"] == "AAPL"
    assert item["data"]["quantity"] == "10"
    assert "ttl" in item
    assert item["ttl"] > item["timestamp"]
