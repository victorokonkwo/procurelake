"""cdc_processing and dbt_transformation definitions (ADR 0029, ADR 0050), rendered as Terraform does.

Checks the structure the selective Gold build depends on: the queue is read after the apply,
nothing queued skips dbt, the queued entries reach the dbt runner unchanged, and only the
entries that were built are removed, after the build succeeded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "orchestration" / "step_functions"
CHECKPOINTS = "procurelake-dev-cdc-checkpoints"
QUEUE_KEY = {"pk": {"S": "gold#cdc"}, "sk": {"S": "pending"}}
SFN = "arn:aws:states:ca-central-1:123456789012:stateMachine"


def render(name: str, values: dict[str, str]) -> dict:
    text = (ROOT / name).read_text()
    assert set(re.findall(r"\$\{(\w+)\}", text)) == set(values), "template placeholders changed"
    for key, value in values.items():
        text = text.replace("${" + key + "}", value)
    return json.loads(text)


def cdc() -> dict:
    return render(
        "cdc_processing.asl.json",
        {
            "apply_cdc_job_name": "procurelake-dev-apply-cdc",
            "checkpoint_table_name": CHECKPOINTS,
            "dbt_state_machine_arn": f"{SFN}:procurelake-dev-dbt-transformation",
            "alerts_topic_arn": "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts",
        },
    )


def dbt() -> dict:
    return render(
        "dbt_transformation.asl.json",
        {
            "cluster_arn": "arn:aws:ecs:ca-central-1:123456789012:cluster/c",
            "task_definition_arn": "arn:aws:ecs:ca-central-1:123456789012:task-definition/t:1",
            "subnet_a": "subnet-a",
            "subnet_b": "subnet-b",
            "security_group_id": "sg-1",
            "tenant_access_check_arn": f"{SFN}:procurelake-dev-tenant-access-check",
        },
    )


def path_from(states: dict, start: str, choose: dict[str, str] | None = None) -> list[str]:
    """States visited from ``start`` following Next, with ``choose`` picking Choice branches."""
    choose = choose or {}
    seen, name = [], start
    while name:
        seen.append(name)
        state = states[name]
        if state["Type"] == "Choice":
            name = choose.get(name, state["Default"])
        elif state["Type"] in ("Succeed", "Fail") or state.get("End"):
            name = None
        else:
            name = state["Next"]
    return seen


def test_happy_path_applies_reads_the_queue_builds_then_clears():
    states = cdc()["States"]
    assert path_from(states, "CountRunningExecutions", {"AnythingQueued": "PrepareSelection"}) == [
        "CountRunningExecutions",
        "AnotherExecutionRunning",
        "ApplyCdc",
        "ReadGoldQueue",
        "AnythingQueued",
        "PrepareSelection",
        "TransformGold",
        "ClearBuiltEntries",
        "Succeeded",
    ]


def test_nothing_queued_skips_dbt():
    states = cdc()["States"]
    queued = states["AnythingQueued"]
    assert queued["Choices"] == [
        {"Variable": "$.gold_queue.Item.entries.SS", "IsPresent": True, "Next": "PrepareSelection"}
    ]
    assert path_from(states, "ReadGoldQueue") == ["ReadGoldQueue", "AnythingQueued", "NothingToBuild"]
    assert states["NothingToBuild"]["Type"] == "Succeed"


def test_queue_is_read_consistently_from_the_checkpoint_table():
    read = cdc()["States"]["ReadGoldQueue"]
    assert read["Resource"] == "arn:aws:states:::dynamodb:getItem"
    assert read["Parameters"] == {"TableName": CHECKPOINTS, "Key": QUEUE_KEY, "ConsistentRead": True}


def test_dbt_gets_the_queued_entries_as_json():
    states = cdc()["States"]
    assert states["PrepareSelection"]["Parameters"] == {
        "entries.$": "$.gold_queue.Item.entries.SS",
        "changed_silver.$": "States.JsonToString($.gold_queue.Item.entries.SS)",
    }
    run = states["TransformGold"]
    assert run["Resource"] == "arn:aws:states:::states:startExecution.sync:2"
    assert run["Parameters"]["Input"]["changed_silver.$"] == "$.selection.changed_silver"
    assert run["Parameters"]["Input"]["run_id.$"] == "$$.Execution.Name"


def test_only_the_built_entries_are_removed_and_only_after_success():
    states = cdc()["States"]
    assert states["TransformGold"]["Next"] == "ClearBuiltEntries"
    assert states["TransformGold"]["Catch"][0]["Next"] == "NotifyFailure"  # failed build keeps the queue
    clear = states["ClearBuiltEntries"]
    assert clear["Resource"] == "arn:aws:states:::dynamodb:updateItem"
    params = clear["Parameters"]
    assert params["TableName"] == CHECKPOINTS and params["Key"] == QUEUE_KEY
    assert params["UpdateExpression"] == "DELETE #e :built"
    assert params["ExpressionAttributeNames"] == {"#e": "entries"}
    assert params["ExpressionAttributeValues"] == {":built": {"SS.$": "$.selection.entries"}}


def test_every_task_after_the_skip_check_alerts_on_failure():
    states = cdc()["States"]
    for name in ("ApplyCdc", "ReadGoldQueue", "TransformGold", "ClearBuiltEntries"):
        assert states[name]["Catch"] == [
            {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "NotifyFailure"}
        ], name
    assert states["NotifyFailure"]["Next"] == "Failed" and states["Failed"]["Type"] == "Fail"


def test_dbt_transformation_defaults_to_a_full_build_and_passes_the_selection_to_the_runner():
    states = dbt()["States"]
    assert path_from(states, "ApplyDefaults")[:3] == ["ApplyDefaults", "MergeInput", "RunDbt"]
    defaults = states["ApplyDefaults"]["Parameters"]
    assert defaults == {
        "defaults": {"run_id.$": "$$.Execution.Name", "changed_silver": "all"},
        "input.$": "$",
    }
    merge = states["MergeInput"]
    assert merge["Parameters"] == {"merged.$": "States.JsonMerge($.defaults, $.input, false)"}
    assert merge["OutputPath"] == "$.merged"
    env = states["RunDbt"]["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"]
    assert {"Name": "PROCURELAKE_DBT_CHANGED_SILVER", "Value.$": "$.changed_silver"} in env
    assert {"Name": "PROCURELAKE_RUN_ID", "Value.$": "$.run_id"} in env
    # A selective build is still followed by the tenant access check (ADR 0030).
    assert states["CheckExitCode"]["Choices"][0]["Next"] == "CheckTenantAccess"
