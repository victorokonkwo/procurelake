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


# ----------------------------------------------------------------------------- CDC selection (ADR 0050)
REPO = Path(__file__).resolve().parents[3]
PROJECT = REPO / "dbt"  # the image copies dbt/ to /app/dbt
BATCH = "20261114T120000Z-0a1b2c3d"


def _changed(*tables: str) -> str:
    import json

    return json.dumps([f"{t}#{BATCH}-{i}" for i, t in enumerate(tables)])


def test_allow_list_is_the_dbt_silver_source_and_covers_every_cdc_table():
    from procurelake.lakehouse.silver.resources import load_silver_specs

    allowed = entry.cdc_source_tables(PROJECT)
    cdc_tables = {spec.silver_table for spec in load_silver_specs().values()}
    assert cdc_tables <= allowed, "every table the CDC apply job queues must be selectable"
    assert "silver_usaspending_contract_awards" not in allowed  # other sources are not CDC-fed


def test_changed_tables_build_their_downstream_nodes_with_tests():
    env = {"PROCURELAKE_DBT_CHANGED_SILVER": _changed("silver_invoices", "silver_awards", "silver_invoices")}
    cmd = entry.dbt_command(env, PROJECT)
    assert cmd[:2] == ["dbt", "build"]
    assert cmd[-3:] == ["--select", "source:silver.silver_awards+", "source:silver.silver_invoices+"]
    assert "--indirect-selection" not in cmd  # dbt's default (eager) keeps tests of selected nodes


@pytest.mark.parametrize("value", ["", "all", "  all  "])
def test_all_or_empty_means_the_whole_project(value):
    cmd = entry.dbt_command({"PROCURELAKE_DBT_CHANGED_SILVER": value}, PROJECT)
    assert "--select" not in cmd and cmd[:2] == ["dbt", "build"]


def test_a_long_queue_falls_back_to_a_full_build():
    many = _changed(*(["silver_awards"] * (entry.MAX_QUEUE_ENTRIES + 1)))
    cmd = entry.dbt_command({"PROCURELAKE_DBT_CHANGED_SILVER": many}, PROJECT)
    assert "--select" not in cmd


@pytest.mark.parametrize(
    "value",
    [
        '["silver_awards"]',  # no batch id
        '["silver_awards#b; rm -rf /"]',
        '["silver_awards#b $(id)"]',
        '["silver_awards+#b"]',
        '["silver_usaspending_contract_awards#b"]',  # not a table of the CDC source
        '["silver_contract_intelligence#b"]',
        '["../silver_awards#b"]',
        '"silver_awards#b"',  # not an array
        "[]",
        "[1]",
        "not json",
        "silver_awards#b",
    ],
)
def test_queue_entries_are_validated_strictly(value):
    with pytest.raises(ValueError):
        entry.dbt_command({"PROCURELAKE_DBT_CHANGED_SILVER": value}, PROJECT)


@pytest.mark.parametrize(
    "env",
    [
        {"PROCURELAKE_DBT_COMMAND": "test"},
        {"PROCURELAKE_DBT_ARGS": "--select fct_invoice"},
        {"PROCURELAKE_DBT_ARGS": "-s fct_invoice"},
        {"PROCURELAKE_DBT_ARGS": "--exclude=fct_invoice"},
    ],
)
def test_a_cdc_selection_cannot_be_combined_with_another_command_or_selection(env):
    with pytest.raises(ValueError):
        entry.dbt_command({"PROCURELAKE_DBT_CHANGED_SILVER": _changed("silver_awards"), **env}, PROJECT)


def test_a_cdc_selection_still_refuses_a_full_refresh():
    env = {
        "PROCURELAKE_DBT_CHANGED_SILVER": _changed("silver_awards"),
        "PROCURELAKE_DBT_ARGS": "--full-refresh",
    }
    with pytest.raises(ValueError, match="full refresh"):
        entry.dbt_command(env, PROJECT)
