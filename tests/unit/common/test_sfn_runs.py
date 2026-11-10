"""start_and_wait for Step Functions (botocore Stubber; no AWS calls)."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber

from procurelake.common.sfn_runs import start_and_wait

SM = "arn:aws:states:ca-central-1:123456789012:stateMachine:m"
EX = "arn:aws:states:ca-central-1:123456789012:execution:m:e1"
T0 = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)


@pytest.fixture
def sfn():
    client = boto3.client(
        "stepfunctions", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub:
        yield client, stub


def _describe(stub, status, **extra):
    base = {"executionArn": EX, "stateMachineArn": SM, "status": status, "startDate": T0}
    stub.add_response("describe_execution", {**base, **extra}, {"executionArn": EX})


def test_waits_for_success_and_parses_output(sfn):
    client, stub = sfn
    stub.add_response(
        "start_execution", {"executionArn": EX, "startDate": T0}, {"stateMachineArn": SM, "input": '{"a": 1}'}
    )
    _describe(stub, "RUNNING")
    _describe(stub, "SUCCEEDED", stopDate=T0, output='{"ok": true}')
    result = start_and_wait(client, SM, {"a": 1}, sleep=lambda _: None)
    assert result.succeeded and result.output == {"ok": True}


def test_reports_failure_cause(sfn):
    client, stub = sfn
    stub.add_response(
        "start_execution", {"executionArn": EX, "startDate": T0}, {"stateMachineArn": SM, "input": "{}"}
    )
    _describe(stub, "FAILED", stopDate=T0, error="CdcProcessingFailed", cause="See the execution history")
    result = start_and_wait(client, SM, {}, sleep=lambda _: None)
    assert (result.succeeded, result.error) == (False, "CdcProcessingFailed")
