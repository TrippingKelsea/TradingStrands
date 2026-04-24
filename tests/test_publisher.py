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


def test_put_strategy() -> None:
    publisher, mock_table = _make_publisher()

    result = publisher.put_strategy(
        name="Momentum AAPL",
        markdown="## Buy when RSI < 30",
        symbols=["AAPL", "MSFT"],
        capital="5000",
        strategy_id="test123",
    )

    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args.kwargs["Item"]

    assert item["pk"] == "STRATEGY#test123"
    assert item["strategy_id"] == "test123"
    assert item["name"] == "Momentum AAPL"
    assert item["markdown"] == "## Buy when RSI < 30"
    assert item["symbols"] == ["AAPL", "MSFT"]
    assert item["capital"] == "5000"
    assert item["status"] == "active"
    assert "created_at" in item
    assert result["pk"] == "STRATEGY#test123"


def test_get_strategies() -> None:
    publisher, mock_table = _make_publisher()

    mock_table.scan.return_value = {
        "Items": [
            {"pk": "STRATEGY#a", "strategy_id": "a", "name": "Strat A", "status": "active"},
            {"pk": "STRATEGY#b", "strategy_id": "b", "name": "Strat B", "status": "paused"},
        ],
    }

    result = publisher.get_strategies()
    assert len(result) == 2
    assert result[0]["strategy_id"] == "a"
    assert result[1]["strategy_id"] == "b"

    mock_table.scan.assert_called_once()
    call_kwargs = mock_table.scan.call_args.kwargs
    assert "FilterExpression" in call_kwargs


def test_get_strategy_found() -> None:
    publisher, mock_table = _make_publisher()

    mock_table.get_item.return_value = {
        "Item": {"pk": "STRATEGY#abc", "strategy_id": "abc", "name": "Test"},
    }

    result = publisher.get_strategy("abc")
    assert result is not None
    assert result["strategy_id"] == "abc"


def test_get_strategy_not_found() -> None:
    publisher, mock_table = _make_publisher()
    mock_table.get_item.return_value = {}

    result = publisher.get_strategy("nonexistent")
    assert result is None


def test_delete_strategy() -> None:
    publisher, mock_table = _make_publisher()

    publisher.delete_strategy("abc")
    mock_table.delete_item.assert_called_once_with(Key={"pk": "STRATEGY#abc"})


def test_publish_snapshot_with_telemetry() -> None:
    publisher, mock_table = _make_publisher()

    risk = RiskManager(RiskConfig())
    telemetry = {
        "uptime_seconds": 300,
        "tick_rate_per_min": 12.0,
        "active_bots": 2,
        "watched_symbols": ["AAPL", "MSFT"],
        "broker_status": "connected",
        "broker_last_error": "",
        "trades_executed": 5,
        "trades_rejected": 1,
        "tick_interval": 5.0,
    }

    publisher.publish_snapshot(
        tick=10,
        prices={},
        ledgers={},
        risk_manager=risk,
        telemetry=telemetry,
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["telemetry"]["uptime_seconds"] == 300
    assert item["telemetry"]["broker_status"] == "connected"
    assert item["telemetry"]["active_bots"] == 2


def test_set_halt() -> None:
    publisher, mock_table = _make_publisher()

    publisher.set_halt(True)
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "CONTROL"
    assert item["desk_halted"] is True


def test_get_halt_true() -> None:
    publisher, mock_table = _make_publisher()
    mock_table.get_item.return_value = {
        "Item": {"pk": "CONTROL", "desk_halted": True},
    }

    assert publisher.get_halt() is True


def test_get_halt_false_no_item() -> None:
    publisher, mock_table = _make_publisher()
    mock_table.get_item.return_value = {}

    assert publisher.get_halt() is False


def test_publish_snapshot_with_agents() -> None:
    publisher, mock_table = _make_publisher()

    risk = RiskManager(RiskConfig())
    agents = [
        {"name": "Orchestrator", "type": "core", "status": "running", "detail": "ok"},
        {"name": "RiskManager", "type": "core", "status": "running", "detail": "ok"},
        {"name": "strategy-0", "type": "strategy", "status": "running",
         "detail": "symbols=['AAPL']"},
    ]

    publisher.publish_snapshot(
        tick=5,
        prices={},
        ledgers={},
        risk_manager=risk,
        agents=agents,
    )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert "agents" in item
    assert len(item["agents"]) == 3
    assert item["agents"][0]["name"] == "Orchestrator"
    assert item["agents"][2]["type"] == "strategy"
