"""Unit tests for the destroy-dev safeguards (ADR 0038). AWS clients are in-test stubs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from procurelake.operations.teardown import (
    TeardownRefused,
    active_work,
    backup_tables,
    check_roots,
    is_kept,
    managed_addresses,
    select_targets,
    verify_destroy_plan,
)


def test_roots_come_back_in_dependency_order() -> None:
    assert check_roots(["data_platform", "cdc", "security", "observability"]) == [
        "cdc",
        "observability",
        "security",
        "data_platform",
    ]


@pytest.mark.parametrize("root", ["foundation", "bootstrap"])
def test_state_and_lake_roots_are_refused(root: str) -> None:
    with pytest.raises(TeardownRefused, match=root):
        check_roots(["security", root])


def test_unknown_root_is_refused() -> None:
    with pytest.raises(TeardownRefused, match="unknown"):
        check_roots(["prod"])


@pytest.mark.parametrize(
    "address",
    [
        "aws_dynamodb_table.cdc_checkpoints",
        "aws_dynamodb_table.ai_review",
        "module.dbt_runner.aws_ecr_repository.this",
        "module.dbt_runner.aws_ecr_lifecycle_policy.this",
        'aws_cloudwatch_log_group.glue["/aws-glue/jobs/error"]',
        "module.dbt_runner.aws_cloudwatch_log_group.this",
    ],
)
def test_data_holding_resources_are_kept(address: str) -> None:
    assert is_kept(address)


@pytest.mark.parametrize(
    "address",
    [
        "aws_glue_job.silver",
        'aws_iam_role.tenant["tenant_alder"]',
        "aws_iam_role_policy.dynamodb_table_access",  # name mentions a kept type, type differs
        'module.tenant_workgroup["tenant_alder"].aws_athena_workgroup.this',
        "aws_sfn_state_machine.cdc_processing",
    ],
)
def test_everything_else_is_destroyed(address: str) -> None:
    assert not is_kept(address)


def test_data_sources_are_not_targets() -> None:
    state = [
        "data.aws_caller_identity.current",
        "data.terraform_remote_state.foundation",
        'module.net.data.aws_availability_zones.this["a"]',
        "aws_glue_job.silver",
        "",
    ]
    assert managed_addresses(state) == ["aws_glue_job.silver"]


def test_selection_splits_targets_and_kept() -> None:
    sel = select_targets(["aws_glue_job.silver", "aws_dynamodb_table.ai_review"])
    assert sel.targets == ["aws_glue_job.silver"]
    assert sel.kept == ["aws_dynamodb_table.ai_review"]


def _plan(*changes: tuple[str, list[str]]) -> dict:
    return {
        "resource_changes": [
            {"address": a, "mode": "managed", "change": {"actions": acts}} for a, acts in changes
        ]
    }


def test_clean_destroy_plan_passes() -> None:
    check = verify_destroy_plan(
        _plan(("aws_glue_job.silver", ["delete"]), ("aws_dynamodb_table.ai_review", ["no-op"])),
        kept=["aws_dynamodb_table.ai_review"],
    )
    assert check.deletes == ["aws_glue_job.silver"]


def test_plan_deleting_a_kept_resource_is_refused() -> None:
    with pytest.raises(TeardownRefused, match="kept resource"):
        verify_destroy_plan(
            _plan(("aws_dynamodb_table.cdc_checkpoints", ["delete"])),
            kept=["aws_dynamodb_table.cdc_checkpoints"],
        )


@pytest.mark.parametrize("actions", [["create"], ["update"], ["delete", "create"]])
def test_plan_that_changes_anything_else_is_refused(actions: list[str]) -> None:
    with pytest.raises(TeardownRefused, match="must only delete"):
        verify_destroy_plan(_plan(("aws_glue_job.silver", actions)), kept=[])


class _Pages:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **_: object) -> list[dict]:
        return self._pages


class _Glue:
    def get_paginator(self, _: str) -> _Pages:
        return _Pages([{"Jobs": [{"Name": "procurelake-dev-silver"}, {"Name": "other-job"}]}])

    def get_job_runs(self, JobName: str, MaxResults: int) -> dict:
        assert JobName == "procurelake-dev-silver"
        return {
            "JobRuns": [{"Id": "jr_1", "JobRunState": "RUNNING"}, {"Id": "jr_0", "JobRunState": "SUCCEEDED"}]
        }


class _Sfn:
    def get_paginator(self, _: str) -> _Pages:
        return _Pages(
            [{"stateMachines": [{"name": "procurelake-dev-cdc-processing", "stateMachineArn": "arn:sm"}]}]
        )

    def list_executions(self, **kwargs: object) -> dict:
        assert kwargs["statusFilter"] == "RUNNING"
        return {"executions": []}


def test_active_work_lists_running_glue_runs_only_for_the_prefix() -> None:
    assert active_work(_Glue(), _Sfn()) == ["glue procurelake-dev-silver jr_1 RUNNING"]


class _Dynamo:
    def get_paginator(self, _: str) -> _Pages:
        return _Pages(
            [
                {"Items": [{"pk": {"S": "a"}, "sk": {"S": "1"}}]},
                {"Items": [{"pk": {"S": "b"}, "sk": {"N": "2"}}]},
            ]
        )


class _S3:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kwargs: object) -> None:
        self.puts.append(kwargs)


def test_backup_writes_every_item_losslessly() -> None:
    s3 = _S3()
    out = backup_tables(_Dynamo(), s3, ["t1"], "bucket", datetime(2026, 11, 12, 3, 4, 5, tzinfo=UTC))
    assert out[0].items == 2
    assert out[0].s3_uri == "s3://bucket/teardown/20261112T030405Z/t1.jsonl"
    lines = s3.puts[0]["Body"].decode().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"pk": {"S": "a"}, "sk": {"S": "1"}},
        {"pk": {"S": "b"}, "sk": {"N": "2"}},
    ]
