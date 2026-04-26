"""Shared moto-backed S3 bucket fixture."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def s3_client() -> Iterator[object]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket="trading-strands-agent-memory-test",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        yield client
