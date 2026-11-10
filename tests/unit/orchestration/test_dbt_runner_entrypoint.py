"""dbt runner container entrypoint (moto S3 test double; no AWS calls)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ENTRY = Path(__file__).resolve().parents[3] / "jobs" / "containers" / "dbt_runner" / "entrypoint.py"
spec = importlib.util.spec_from_file_location("dbt_runner_entrypoint", ENTRY)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def test_command_defaults_to_build_and_appends_args():
    cmd = entry.dbt_command({"PROCURELAKE_DBT_ARGS": "--select fct_invoice"})
    assert cmd[:2] == ["dbt", "build"] and cmd[-2:] == ["--select", "fct_invoice"]
    assert "--target-path" in cmd and str(entry.TARGET) in cmd


@pytest.mark.parametrize("command", ["seed", "run-operation", "build; rm"])
def test_command_allow_list(command):
    with pytest.raises(ValueError):
        entry.dbt_command({"PROCURELAKE_DBT_COMMAND": command})


@pytest.mark.parametrize(
    "env",
    [
        {"PROCURELAKE_DBT_ARGS": "--full-refresh"},
        {"PROCURELAKE_DBT_ARGS": "--select mart_awarded_value -f"},
        {"PROCURELAKE_DBT_ARGS": "-xf"},
        {"DBT_FULL_REFRESH": "true"},
        {"DBT_FULL_REFRESH": "1"},
    ],
)
def test_full_refresh_is_refused(env):
    # A full refresh drops the Gold tables and, with them, their Lake Formation grants.
    with pytest.raises(ValueError, match=r"full refresh|DBT_FULL_REFRESH"):
        entry.dbt_command(env)


def test_non_refresh_flags_are_kept():
    cmd = entry.dbt_command({"PROCURELAKE_DBT_ARGS": "-x --select fct_invoice", "DBT_FULL_REFRESH": "false"})
    assert cmd[-3:] == ["-x", "--select", "fct_invoice"]


def test_artifacts_are_uploaded_per_run(tmp_path):
    (tmp_path / "run_results.json").write_text("{}")
    (tmp_path / "manifest.json").write_text("{}")
    with mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        s3.create_bucket(Bucket="art", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
        keys = entry.upload_artifacts(s3, "art", "exec-123", tmp_path)
        assert keys == ["dbt/runs/exec-123/run_results.json", "dbt/runs/exec-123/manifest.json"]
        assert s3.get_object(Bucket="art", Key=keys[0])["Body"].read() == b"{}"
    with pytest.raises(ValueError):
        entry.upload_artifacts(None, "art", "../escape", tmp_path)
