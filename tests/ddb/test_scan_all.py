"""Tests for the shared scan_all helper.

This module is the single sanctioned caller of table.scan in the
codebase. Its contract: exhaust the scan by following LastEvaluatedKey
and return every matching row, regardless of how many pages DDB
decides to serve.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Attr
from moto import mock_aws

from trading_strands.ddb import scan_all


def _table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("t")


def test_single_page_returns_all_matches() -> None:
    with mock_aws():
        t = _table()
        t.put_item(Item={"pk": "STRATEGY#a"})
        t.put_item(Item={"pk": "STRATEGY#b"})
        t.put_item(Item={"pk": "OTHER#x"})  # non-match, filtered out

        items = scan_all(t, Attr("pk").begins_with("STRATEGY#"))
        pks = sorted(i["pk"] for i in items)
        assert pks == ["STRATEGY#a", "STRATEGY#b"]


def test_multi_page_follows_last_evaluated_key() -> None:
    """Simulated by replacing table.scan with a driver that serves our
    three rows one per page — deterministic reproduction of the
    production bug where a filter scan drops matches across page
    boundaries."""

    with mock_aws():
        t = _table()
        # Sequencer: the paginated_scan doesn't need to consult the
        # real moto table at all. It just feeds back the rows we
        # prepared, one per call, setting LastEvaluatedKey until the
        # last.
        rows = [{"pk": "X#1"}, {"pk": "X#2"}, {"pk": "X#3"}]
        calls: list[dict[str, Any]] = []

        def paginated_scan(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            idx = len(calls) - 1
            if idx >= len(rows):
                return {"Items": []}
            last = idx == len(rows) - 1
            resp: dict[str, Any] = {"Items": [rows[idx]]}
            if not last:
                resp["LastEvaluatedKey"] = {"pk": rows[idx]["pk"]}
            return resp

        t.scan = paginated_scan  # type: ignore[method-assign]
        items = scan_all(t, Attr("pk").begins_with("X#"))

        pks = sorted(i["pk"] for i in items)
        assert pks == ["X#1", "X#2", "X#3"]
        # Three pages walked: first two had LastEvaluatedKey, third didn't.
        assert len(calls) == 3
        assert "ExclusiveStartKey" in calls[1]
        assert "ExclusiveStartKey" in calls[2]


def test_warns_when_pagination_occurs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Chronic pagination is a sign the access pattern is wrong (a
    missing GSI or shard). A WARN log per call gives operators a
    signal before the pattern causes a user-visible problem.

    structlog writes to stdout by default in this codebase, so we use
    capsys rather than caplog — the event name must appear in the
    captured stdout stream."""

    with mock_aws():
        t = _table()

        def paginated_scan(**kwargs: Any) -> dict[str, Any]:
            if "ExclusiveStartKey" in kwargs:
                return {"Items": [{"pk": "X#2"}]}
            return {
                "Items": [{"pk": "X#1"}],
                "LastEvaluatedKey": {"pk": "X#1"},
            }

        t.scan = paginated_scan  # type: ignore[method-assign]
        scan_all(t, Attr("pk").begins_with("X#"))

        out = capsys.readouterr().out
        assert "ddb.scan_all.paginated" in out


def test_single_page_does_not_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No LastEvaluatedKey → no warning. The signal only fires when a
    naive caller would have dropped rows."""

    with mock_aws():
        t = _table()
        t.put_item(Item={"pk": "X#1"})
        scan_all(t, Attr("pk").begins_with("X#"))
        out = capsys.readouterr().out
        assert "ddb.scan_all.paginated" not in out


def test_empty_table_returns_empty_list() -> None:
    with mock_aws():
        t = _table()
        assert scan_all(t, Attr("pk").begins_with("X#")) == []
