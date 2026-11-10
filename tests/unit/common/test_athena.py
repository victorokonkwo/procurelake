"""Unit tests for the Athena runner using botocore's Stubber (no AWS calls)."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from procurelake.common.athena import AthenaQueryError, run_query

QID = "11111111-2222-3333-4444-555555555555"


def _execution(state: str, reason: str | None = None) -> dict:
    status = {"State": state}
    if reason:
        status["StateChangeReason"] = reason
    return {
        "QueryExecution": {
            "QueryExecutionId": QID,
            "Status": status,
            "Statistics": {"DataScannedInBytes": 1234, "EngineExecutionTimeInMillis": 56},
            "ResultConfiguration": {"OutputLocation": f"s3://results/athena_results/wg/{QID}.csv"},
        }
    }


@pytest.fixture
def athena():
    client = boto3.client(
        "athena", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub:
        yield client, stub


def test_waits_for_success_and_strips_header_row(athena):
    client, stub = athena
    stub.add_response(
        "start_query_execution",
        {"QueryExecutionId": QID},
        {"QueryString": "SELECT 1", "WorkGroup": "wg", "QueryExecutionContext": {"Database": "db"}},
    )
    stub.add_response("get_query_execution", _execution("RUNNING"), {"QueryExecutionId": QID})
    stub.add_response("get_query_execution", _execution("SUCCEEDED"), {"QueryExecutionId": QID})
    stub.add_response(
        "get_query_results",
        {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": "n", "Type": "integer"}]},
                "Rows": [{"Data": [{"VarCharValue": "n"}]}, {"Data": [{"VarCharValue": "1"}]}],
            }
        },
        {"QueryExecutionId": QID},
    )
    sleeps: list[float] = []
    result = run_query(client, "SELECT 1", "wg", "db", sleep=sleeps.append)
    assert result.columns == ["n"]
    assert result.rows == [("1",)]
    assert result.data_scanned_bytes == 1234
    assert sleeps == [1.0]


def test_failed_query_raises_with_reason(athena):
    client, stub = athena
    stub.add_response("start_query_execution", {"QueryExecutionId": QID})
    stub.add_response("get_query_execution", _execution("FAILED", "SYNTAX_ERROR: line 1"))
    with pytest.raises(AthenaQueryError, match="SYNTAX_ERROR") as exc:
        run_query(client, "SELEC 1", "wg", "db", sleep=lambda _: None)
    assert exc.value.query_execution_id == QID


def test_timeout_stops_the_query(athena):
    client, stub = athena
    stub.add_response("start_query_execution", {"QueryExecutionId": QID})
    stub.add_response("get_query_execution", _execution("RUNNING"))
    stub.add_response("stop_query_execution", {}, {"QueryExecutionId": QID})
    with pytest.raises(AthenaQueryError, match="stopped"):
        run_query(client, "SELECT 1", "wg", "db", timeout_seconds=0, sleep=lambda _: None)
