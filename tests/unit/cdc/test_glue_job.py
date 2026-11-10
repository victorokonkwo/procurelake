"""CDC Glue job helpers (botocore Stubber for DMS; no AWS calls)."""

from __future__ import annotations

import ast
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from procurelake.cdc.glue_job import completed_full_loads, parse_args

TASK = "arn:aws:dms:ca-central-1:123456789012:task:T"
TARGET = "arn:aws:dms:ca-central-1:123456789012:endpoint:E"


def _stats(name, state):
    return {"SchemaName": "procurement", "TableName": name, "TableState": state}


def test_completed_full_loads_uses_the_task_target_load_id():
    dms = boto3.client("dms", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(dms) as stub:
        stub.add_response(
            "describe_replication_tasks",
            {"ReplicationTasks": [{"ReplicationTaskArn": TASK, "TargetEndpointArn": TARGET}]},
            {"Filters": [{"Name": "replication-task-arn", "Values": [TASK]}]},
        )
        stub.add_response(
            "describe_endpoints",
            {
                "Endpoints": [
                    {
                        "EndpointArn": TARGET,
                        "S3Settings": {"BucketFolder": "bronze/postgres_cdc/load_id=20261110T130454Z"},
                    }
                ]
            },
            {"Filters": [{"Name": "endpoint-arn", "Values": [TARGET]}]},
        )
        stub.add_response(
            "describe_table_statistics",
            {
                "TableStatistics": [
                    _stats("awards", "Table completed"),
                    _stats("invoices", "Full load"),
                    _stats("tenants", "Table error"),
                ]
            },
            {"ReplicationTaskArn": TASK},
        )
        assert completed_full_loads(dms, TASK) == {"awards": "20261110T130454Z"}  # invoices still loading


def test_parse_args_ignores_glue_arguments():
    args = parse_args(
        [
            "--JOB_NAME",
            "x",
            "--JOB_RUN_ID",
            "jr_1",
            "--raw-bucket",
            "raw",
            "--checkpoint-table",
            "cp",
            "--silver-database",
            "procurelake_dev_silver",
            "--silver-location",
            "s3://lake/silver",
            "--quarantine-root",
            "s3://raw/quarantine",
        ]
    )
    assert (args.job_run_id, args.dms_task_arn, args.cdc_root) == ("jr_1", "", "bronze/postgres_cdc")


@pytest.mark.parametrize("entrypoint", ["apply_cdc.py", "bronze_to_silver.py"])
def test_glue_entrypoints_never_call_sys_exit(entrypoint):
    tree = ast.parse((Path(__file__).resolve().parents[3] / "jobs" / "glue" / entrypoint).read_text())
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "exit"] == []
