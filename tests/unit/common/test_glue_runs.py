"""start_and_wait against botocore's Stubber (no AWS calls)."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from procurelake.common.glue_runs import start_and_wait


@pytest.fixture
def glue():
    client = boto3.client(
        "glue", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub:
        yield client, stub


def _get(stub, state, **extra):
    stub.add_response(
        "get_job_run",
        {"JobRun": {"Id": "jr_1", "JobRunState": state, **extra}},
        {"JobName": "job", "RunId": "jr_1", "PredecessorsIncluded": False},
    )


def test_polls_until_terminal_state(glue):
    client, stub = glue
    stub.add_response("start_job_run", {"JobRunId": "jr_1"}, {"JobName": "job", "Arguments": {"--a": "1"}})
    _get(stub, "STARTING")
    _get(stub, "RUNNING")
    _get(stub, "SUCCEEDED", ExecutionTime=120, DPUSeconds=240.0)
    sleeps = []
    result = start_and_wait(client, "job", {"--a": "1"}, poll_seconds=5, sleep=sleeps.append)
    assert (result.state, result.execution_time_seconds, result.dpu_seconds) == ("SUCCEEDED", 120, 240.0)
    assert result.succeeded and sleeps == [5, 5]


def test_failed_run_reports_error(glue):
    client, stub = glue
    stub.add_response("start_job_run", {"JobRunId": "jr_1"}, {"JobName": "job"})
    _get(stub, "FAILED", ErrorMessage="SilverQualityGateError: blocked")
    result = start_and_wait(client, "job", sleep=lambda _: None)
    assert not result.succeeded and "blocked" in result.error_message


def test_times_out(glue):
    client, stub = glue
    stub.add_response("start_job_run", {"JobRunId": "jr_1"}, {"JobName": "job"})
    _get(stub, "RUNNING")
    _get(stub, "RUNNING")
    ticks = iter([0.0, 5.0, 11.0])
    with pytest.raises(TimeoutError):
        start_and_wait(client, "job", timeout_seconds=10, sleep=lambda _: None, clock=lambda: next(ticks))
