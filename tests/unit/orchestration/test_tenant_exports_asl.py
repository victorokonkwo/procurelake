"""tenant_exports state machine definition (ADR 0030), rendered as Terraform renders it.

The template holds an unquoted ``${exports_json}`` placeholder, so it is not JSON until it is
rendered (it is excluded from pre-commit's check-json for that reason). This test renders it
and checks the structure that tenant isolation depends on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ASL = Path(__file__).resolve().parents[3] / "orchestration" / "step_functions" / "tenant_exports.asl.json"
EXPORTS = [
    {
        "tenant": t,
        "mart": "mart_awarded_value",
        "role_arn": f"arn:aws:iam::123456789012:role/{t}",
        "workgroup": t,
    }
    for t in ("tenant_alder", "tenant_birch")
]


def render() -> dict:
    values = {
        "exports_json": json.dumps(EXPORTS),
        "gold_database": "procurelake_dev_gold",
        "products_bucket": "procurelake-dev-products-123456789012-ca-central-1",
        "alerts_topic_arn": "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts",
    }
    text = ASL.read_text()
    assert set(re.findall(r"\$\{(\w+)\}", text)) == set(values), "template placeholders changed"
    for name, value in values.items():
        text = text.replace("${" + name + "}", value)
    return json.loads(text)


def test_renders_to_valid_json_with_the_exports_list():
    definition = render()
    assert definition["States"]["ListExports"]["Parameters"]["exports"] == EXPORTS


def test_every_s3_or_athena_task_runs_as_the_tenant_role():
    processor = render()["States"]["ExportEach"]["ItemProcessor"]["States"]
    tasks = {name: s for name, s in processor.items() if s["Type"] == "Task"}
    assert set(tasks) == {"Unload", "WriteManifest"}
    for state in tasks.values():
        assert state["Credentials"] == {"RoleArn.$": "$.role_arn"}


def test_unload_writes_parquet_under_the_tenant_prefix():
    states = render()["States"]
    unload = states["ExportEach"]["ItemProcessor"]["States"]["Unload"]["Parameters"]["QueryString.$"]
    assert "UNLOAD (SELECT * FROM procurelake_dev_gold.{})" in unload and "PARQUET" in unload
    prefix = states["ExportEach"]["ItemSelector"]["prefix.$"]
    assert prefix.startswith("States.Format('{}/{}/export_date={}/run={}/', $$.Map.Item.Value.tenant")


def test_manifest_body_is_passed_as_an_object():
    # The aws-sdk:s3:putObject integration serializes a JSON Body itself; wrapping it in
    # States.JsonToString stored a JSON string (live, 2026-11-10).
    write = render()["States"]["ExportEach"]["ItemProcessor"]["States"]["WriteManifest"]["Parameters"]
    assert write["Body.$"] == "$.manifest_doc"


def test_failures_alert_then_fail():
    states = render()["States"]
    assert states["ExportEach"]["Catch"][0]["Next"] == "NotifyFailure"
    assert states["NotifyFailure"]["Next"] == "Failed" and states["Failed"]["Type"] == "Fail"
