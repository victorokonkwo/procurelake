"""tenant_access_check state machine definition (ADR 0030), rendered as Terraform renders it."""

from __future__ import annotations

import json
import re
from pathlib import Path

ASL = (
    Path(__file__).resolve().parents[3] / "orchestration" / "step_functions" / "tenant_access_check.asl.json"
)
CHECKS = [
    {"tenant": t, "mart": m, "role_arn": f"arn:aws:iam::123456789012:role/{t}", "workgroup": t}
    for t in ("tenant_alder", "tenant_birch")
    for m in ("mart_awarded_value", "mart_actual_spend")
]


def render() -> dict:
    values = {
        "checks_json": json.dumps(CHECKS),
        "gold_database": "procurelake_dev_gold",
        "alerts_topic_arn": "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts",
    }
    text = ASL.read_text()
    assert set(re.findall(r"\$\{(\w+)\}", text)) == set(values), "template placeholders changed"
    for name, value in values.items():
        text = text.replace("${" + name + "}", value)
    return json.loads(text)


def test_checks_every_listed_tenant_and_mart():
    assert render()["States"]["ListChecks"]["Parameters"]["checks"] == CHECKS


def test_queries_and_reads_results_as_the_tenant():
    processor = render()["States"]["CheckEach"]["ItemProcessor"]["States"]
    for name in ("QueryAsTenant", "ReadResult"):
        assert processor[name]["Credentials"] == {"RoleArn.$": "$.role_arn"}
    query = processor["QueryAsTenant"]["Parameters"]["QueryString.$"]
    assert "count_if(tenant_id <> \\'{}\\') AS foreign_rows" in query


def test_any_foreign_row_or_error_alerts_and_fails():
    states = render()["States"]
    choice = states["CheckEach"]["ItemProcessor"]["States"]["OnlyOwnRows"]
    assert choice["Choices"] == [
        {"Variable": "$.result.foreign_rows", "StringEquals": "0", "Next": "Isolated"}
    ]
    assert choice["Default"] == "ForeignRowsVisible"
    assert states["CheckEach"]["Catch"][0] == {
        "ErrorEquals": ["States.ALL"],
        "ResultPath": "$.error",
        "Next": "NotifyFailure",
    }
    assert states["NotifyFailure"]["Next"] == "Failed"
