"""Tests for the dashboard FastAPI service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _mock_table() -> MagicMock:
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "pk": "SNAPSHOT",
            "tick": 5,
            "timestamp": 1700000000,
            "prices": {"AAPL": "155.50"},
            "ledgers": {
                "bot-0": {
                    "equity": "10000",
                    "realized_pnl": "50",
                    "drawdown_pct": "0.01",
                    "high_water_mark": "10050",
                    "positions": [
                        {"symbol": "AAPL", "quantity": "10", "burdened_cost_basis": "150"},
                    ],
                },
            },
            "risk": {"desk_halted": False, "halted_bots": []},
        }
    }
    table.query.return_value = {
        "Items": [
            {
                "pk": "EVENT#123",
                "event_type": "trade.executed",
                "timestamp": 1700000000,
                "data": {"symbol": "AAPL", "action": "buy", "quantity": "10"},
            },
        ],
    }
    return table


@patch("trading_strands.dashboard.api.boto3")
def test_health(mock_boto3: MagicMock) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@patch("trading_strands.dashboard.api.boto3")
def test_snapshot(mock_boto3: MagicMock) -> None:
    table = _mock_table()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pk"] == "SNAPSHOT"
    assert data["tick"] == 5
    assert "AAPL" in data["prices"]
    assert "bot-0" in data["ledgers"]
    assert data["risk"]["desk_halted"] is False


@patch("trading_strands.dashboard.api.boto3")
def test_snapshot_empty(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.get_item.return_value = {}
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tick"] == 0
    assert data["prices"] == {}


@patch("trading_strands.dashboard.api.boto3")
def test_events(mock_boto3: MagicMock) -> None:
    table = _mock_table()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["event_type"] == "trade.executed"


@patch("trading_strands.dashboard.api.boto3")
def test_index_serves_html(mock_boto3: MagicMock) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "TradingStrands" in resp.text
    assert "text/html" in resp.headers["content-type"]


@patch("trading_strands.dashboard.api.boto3")
def test_list_strategies(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {"pk": "STRATEGY#a", "strategy_id": "a", "name": "Strat A",
             "status": "active", "created_at": 100},
            {"pk": "STRATEGY#b", "strategy_id": "b", "name": "Strat B",
             "status": "paused", "created_at": 200},
        ],
    }
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Sorted by created_at desc
    assert data[0]["strategy_id"] == "b"
    assert data[1]["strategy_id"] == "a"


@patch("trading_strands.dashboard.api.boto3")
def test_create_strategy(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/api/strategies", json={
        "name": "Test Strategy",
        "markdown": "## Buy low sell high",
        "symbols": ["AAPL", "MSFT"],
        "capital": "5000",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Strategy"
    assert data["symbols"] == ["AAPL", "MSFT"]
    assert data["capital"] == "5000"
    assert data["status"] == "active"
    assert data["pk"].startswith("STRATEGY#")
    table.put_item.assert_called_once()


@patch("trading_strands.dashboard.api.boto3")
def test_update_strategy_status(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.put("/api/strategies/abc/status", json={"status": "paused"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    table.update_item.assert_called_once()


@patch("trading_strands.dashboard.api.boto3")
def test_update_strategy_status_invalid(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.put("/api/strategies/abc/status", json={"status": "invalid"})
    assert resp.status_code == 400


@patch("trading_strands.dashboard.api.boto3")
def test_delete_strategy(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.delete("/api/strategies/abc")
    assert resp.status_code == 204
    table.delete_item.assert_called_once_with(Key={"pk": "STRATEGY#abc"})
