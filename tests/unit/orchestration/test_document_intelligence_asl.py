"""document_intelligence state machine definition (ADR 0045), rendered as Terraform renders it.

Checks the structure the design depends on: extraction runs as the AI extraction role, both
inputs (a landing event, a document id by hand) reach the container only as environment values
the entrypoint validates, and every path ends in a reviewer notice or an alert.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ASL = REPO / "orchestration" / "step_functions" / "document_intelligence.asl.json"
AI_ROLE = "arn:aws:iam::123456789012:role/procurelake-dev-ai-extraction"
VALUES = {
    "runner_cluster_arn": "arn:aws:ecs:ca-central-1:123456789012:cluster/procurelake-dev-task-runner",
    "runner_task_definition_arn": "arn:aws:ecs:ca-central-1:123456789012:task-definition/task-runner:9",
    "runner_security_group_id": "sg-0123",
    "runner_name": "procurelake-dev-task-runner",
    "subnet_a": "subnet-a",
    "subnet_b": "subnet-b",
    "ai_extraction_role_arn": AI_ROLE,
    "alerts_topic_arn": "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts",
}
MANIFEST = (
    "pipeline_manifests/public_contracts/contract_documents/window_start=snapshot/"
    "batch_id=nasa-80nssc20c0001-mod0003-0123456789abcdef/manifest.json"
)


def render() -> dict:
    text = ASL.read_text()
    assert set(re.findall(r"\$\{(\w+)\}", text)) == set(VALUES), "template placeholders changed"
    for name, value in VALUES.items():
        text = text.replace("${" + name + "}", value)
    return json.loads(text)


def _entrypoint():
    path = REPO / "jobs" / "containers" / "task_runner" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("task_runner_entrypoint_asl", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(pass_state: dict, value: str) -> dict[str, str]:
    """The container environment a Pass state builds, with JSONPath values substituted."""
    out = {}
    for item in pass_state["Parameters"]["env"]:
        out[item["Name"]] = item.get("Value", value)
    return out


def test_extraction_runs_in_the_task_runner_as_the_ai_extraction_role():
    extract = render()["States"]["Extract"]
    assert extract["Resource"] == "arn:aws:states:::ecs:runTask.sync"
    overrides = extract["Parameters"]["Overrides"]
    assert overrides["TaskRoleArn"] == AI_ROLE
    assert overrides["ContainerOverrides"] == [{"Name": "task", "Environment.$": "$.env"}]


def test_both_inputs_reach_the_entrypoint_as_validated_environment_values():
    states = render()["States"]
    entry = _entrypoint()
    base = {
        "PROCURELAKE_RAW_BUCKET": "raw",
        "PROCURELAKE_ARTIFACTS_BUCKET": "art",
        "PROCURELAKE_AI_REVIEW_TABLE": "review",
    }
    landing = _env(states["FromLanding"], MANIFEST) | {"PROCURELAKE_RUN_ID": "exec-1"}
    _module, argv = entry.task_argv(base | landing)
    assert argv[argv.index("--document-id") + 1] == "nasa-80nssc20c0001-mod0003"
    by_hand = _env(states["ByHand"], "doe-bnl-desc0012704-mod0002") | {"PROCURELAKE_RUN_ID": "exec-2"}
    _module, argv = entry.task_argv(base | by_hand)
    assert argv[argv.index("--document-id") + 1] == "doe-bnl-desc0012704-mod0002"
    assert "--skip-extracted" in argv


def test_route_rejects_other_input_and_every_path_ends_in_a_notice_or_an_alert():
    states = render()["States"]
    assert states["Route"]["Default"] == "BadInput"
    assert states["Extract"]["Catch"][0]["Next"] == "Alert"
    assert states["CheckExitCode"]["Default"] == "Alert"
    assert states["CheckExitCode"]["Choices"][0]["Next"] == "NotifyReviewer"
    assert states["NotifyReviewer"]["Next"] == "Queued"
    assert states["Alert"]["Next"] == "ExtractionFailed"
    for name in ("NotifyReviewer", "Alert"):
        assert states[name]["Parameters"]["TopicArn"] == VALUES["alerts_topic_arn"]


def test_failed_runs_are_retried_but_not_forever():
    retries = render()["States"]["Extract"]["Retry"]
    task_failed = [r for r in retries if r["ErrorEquals"] == ["States.TaskFailed"]]
    assert len(task_failed) == 1 and task_failed[0]["MaxAttempts"] <= 3
