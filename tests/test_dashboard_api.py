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
    table.scan.return_value = {
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
def test_create_strategy_no_symbols(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/api/strategies", json={
        "name": "Volume Scanner",
        "markdown": "## Select top stocks by volume\nBuy the dip",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Volume Scanner"
    assert data["symbols"] == []
    assert data["capital"] == "1000"
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
def test_get_strategy(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "pk": "STRATEGY#abc",
            "strategy_id": "abc",
            "name": "Test",
            "markdown": "## Buy low",
            "symbols": ["AAPL"],
            "capital": "1000",
        },
    }
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/strategies/abc")
    assert resp.status_code == 200
    assert resp.json()["strategy_id"] == "abc"
    assert resp.json()["markdown"] == "## Buy low"


@patch("trading_strands.dashboard.api.boto3")
def test_get_strategy_not_found(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.get_item.return_value = {}
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/strategies/nonexistent")
    assert resp.status_code == 404


@patch("trading_strands.dashboard.api.boto3")
def test_update_strategy(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": {
            "pk": "STRATEGY#abc",
            "strategy_id": "abc",
            "name": "Updated Name",
            "markdown": "## Updated",
            "symbols": ["MSFT"],
            "capital": "2000",
        },
    }
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.put("/api/strategies/abc", json={
        "name": "Updated Name",
        "markdown": "## Updated",
        "symbols": ["MSFT"],
        "capital": "2000",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["symbols"] == ["MSFT"]
    table.update_item.assert_called_once()


@patch("trading_strands.dashboard.api.boto3")
def test_update_strategy_partial(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    table.update_item.return_value = {
        "Attributes": {"pk": "STRATEGY#abc", "name": "New Name"},
    }
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.put("/api/strategies/abc", json={"name": "New Name"})
    assert resp.status_code == 200
    # Should only update name + updated_at
    call_kwargs = table.update_item.call_args.kwargs
    assert "name" not in call_kwargs["UpdateExpression"] or "#n" in call_kwargs["UpdateExpression"]
    assert ":md" not in call_kwargs["ExpressionAttributeValues"]


@patch("trading_strands.dashboard.api.boto3")
def test_delete_strategy(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.delete("/api/strategies/abc")
    assert resp.status_code == 204
    table.delete_item.assert_called_once_with(Key={"pk": "STRATEGY#abc"})


@patch("trading_strands.dashboard.api.boto3")
def test_halt_trading(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/api/halt")
    assert resp.status_code == 200
    assert resp.json()["status"] == "halted"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "CONTROL"
    assert item["desk_halted"] is True


@patch("trading_strands.dashboard.api.boto3")
def test_unhalt_trading(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/api/unhalt")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "CONTROL"
    assert item["desk_halted"] is False


@patch("trading_strands.dashboard.api.boto3")
def test_telemetry(mock_boto3: MagicMock) -> None:
    table = MagicMock()
    # describe_table
    table.meta.client.describe_table.return_value = {
        "Table": {
            "TableStatus": "ACTIVE",
            "ItemCount": 42,
            "TableSizeBytes": 8192,
        },
    }
    table.table_name = "trading-strands-state"
    # snapshot with telemetry
    table.get_item.return_value = {
        "Item": {
            "pk": "SNAPSHOT",
            "tick": 100,
            "timestamp": 1700000000,
            "telemetry": {
                "uptime_seconds": 600,
                "tick_rate_per_min": 12.0,
                "active_bots": 2,
                "watched_symbols": ["AAPL", "MSFT"],
                "broker_status": "connected",
                "broker_last_error": "",
                "trades_executed": 5,
                "trades_rejected": 1,
                "tick_interval": 5.0,
            },
        },
    }
    # scan is called twice: first for strategies, then for events count
    table.scan.side_effect = [
        {
            "Items": [
                {"pk": "STRATEGY#a", "status": "active"},
                {"pk": "STRATEGY#b", "status": "paused"},
            ],
        },
        {"Count": 7},
    ]
    mock_boto3.resource.return_value.Table.return_value = table

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/telemetry")
    assert resp.status_code == 200
    data = resp.json()

    assert data["dynamodb"]["status"] == "ok"
    assert data["dynamodb"]["table_status"] == "ACTIVE"
    assert data["dynamodb"]["item_count"] == 42

    assert data["trading_service"]["last_tick"] == 100
    assert data["trading_service"]["telemetry"]["active_bots"] == 2
    assert data["trading_service"]["telemetry"]["broker_status"] == "connected"

    assert data["strategies"]["total"] == 2
    assert data["strategies"]["by_status"]["active"] == 1
    assert data["strategies"]["by_status"]["paused"] == 1

    assert data["events"]["recent_count"] == 7

    assert data["dashboard"]["status"] == "ok"
