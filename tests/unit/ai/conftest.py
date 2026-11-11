"""Fixtures for the AI pipeline tests: a moto DynamoDB review table (test double)."""

from __future__ import annotations

import boto3
import pytest
from ai_doubles import REVIEW_TABLE
from moto import mock_aws


@pytest.fixture
def review_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")
        client.create_table(
            TableName=REVIEW_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="ca-central-1").Table(REVIEW_TABLE)
