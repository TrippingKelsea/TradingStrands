"""Tests for the per-org Alpaca secrets store."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from trading_strands.alpaca_secrets.store import (
    AlpacaSecretsStore,
    secret_name_for,
)


@pytest.fixture
def sm_client() -> Iterator[object]:
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-west-2")


def test_secret_name_format() -> None:
    assert secret_name_for("abc123") == "trading-strands/org/abc123/alpaca"


def test_status_missing_returns_not_configured(sm_client: object) -> None:
    store = AlpacaSecretsStore(sm_client)
    status = store.status("org_missing")
    assert status.configured is False
    assert status.paper is None


def test_upsert_and_status(sm_client: object) -> None:
    store = AlpacaSecretsStore(sm_client)
    store.upsert("org_a", api_key="K1", secret_key="S1", paper=True)

    status = store.status("org_a")
    assert status.configured is True
    assert status.paper is True


def test_upsert_overwrites(sm_client: object) -> None:
    """Second upsert updates the existing secret."""

    store = AlpacaSecretsStore(sm_client)
    store.upsert("org_a", api_key="K1", secret_key="S1", paper=True)
    store.upsert("org_a", api_key="K2", secret_key="S2", paper=False)

    status = store.status("org_a")
    assert status.configured is True
    assert status.paper is False


def test_delete_removes_secret(sm_client: object) -> None:
    store = AlpacaSecretsStore(sm_client)
    store.upsert("org_a", api_key="K", secret_key="S")
    assert store.status("org_a").configured is True
    store.delete("org_a")
    assert store.status("org_a").configured is False


def test_delete_missing_is_noop(sm_client: object) -> None:
    store = AlpacaSecretsStore(sm_client)
    store.delete("never_existed")  # must not raise
    assert store.status("never_existed").configured is False


def test_status_handles_invalid_json(sm_client: object) -> None:
    """If someone writes a malformed secret outside the store, we report
    as not-configured rather than crashing the dashboard."""

    sm_client.create_secret(  # type: ignore[attr-defined]
        Name=secret_name_for("broken"),
        SecretString="this is not json",
    )
    store = AlpacaSecretsStore(sm_client)
    assert store.status("broken").configured is False


def test_status_handles_missing_keys_in_payload(sm_client: object) -> None:
    """Secret exists but lacks the required keys — treat as not configured."""

    import json as _json

    sm_client.create_secret(  # type: ignore[attr-defined]
        Name=secret_name_for("partial"),
        SecretString=_json.dumps({"ALPACA_API_KEY": "only-key"}),
    )
    store = AlpacaSecretsStore(sm_client)
    assert store.status("partial").configured is False
