"""Shared fixtures for tenancy tests — moto-backed DynamoDB table."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def table() -> Iterator[object]:
    """Fresh in-memory DynamoDB table per test. Matches the production
    schema: single `pk` partition key, pay-per-request."""

    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName="trading-strands-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client.Table("trading-strands-state")
