"""Safeguarded teardown of the dev compute roots (`make destroy-dev`, ADR 0038).

What is destroyed and what is kept is decided here, in pure functions, so it can be tested
without AWS or Terraform:

- **Roots, in order:** ``cdc``, ``observability``, ``security``, ``data_platform``. Each later
  root is read by the earlier ones through remote state, so dependents go first.
  ``bootstrap`` (Terraform state) and ``foundation`` (lake buckets, KMS, Glue databases,
  budget) are never touched: :func:`check_roots` refuses them.
- **Kept inside a destroyed root:** resources that hold data or evidence and cost nothing idle.
  The DynamoDB tables (CDC checkpoints and leases, AI review decisions), the dbt image
  repository and its lifecycle policy, and every CloudWatch log group. Terraform has no
  ``-exclude``, so the destroy is planned with ``-target`` for every other resource, and the
  saved plan is then checked (:func:`verify_destroy_plan`): it must delete nothing kept and
  create or update nothing.
- **Never with a writer running:** :func:`active_work` lists Glue job runs and Step Functions
  executions still in progress; the CLI refuses while there are any.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ROOT_ORDER: tuple[str, ...] = ("cdc", "observability", "security", "data_platform")
PROTECTED_ROOTS: frozenset[str] = frozenset({"bootstrap", "foundation"})
NAME_PREFIX = "procurelake-dev"

# Resource addresses matched by these patterns are kept. Matched against the address without
# its instance key (``aws_dynamodb_table.ai_review``, ``module.x.aws_cloudwatch_log_group.this``).
KEEP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|\.)aws_dynamodb_table\.[^.]+$"),
    re.compile(r"(^|\.)aws_ecr_repository\.[^.]+$"),
    re.compile(r"(^|\.)aws_ecr_lifecycle_policy\.[^.]+$"),
    re.compile(r"(^|\.)aws_cloudwatch_log_group\.[^.]+$"),
)

ACTIVE_GLUE_STATES = frozenset({"STARTING", "RUNNING", "STOPPING", "WAITING"})


class TeardownRefused(RuntimeError):
    """A safeguard refused the teardown; nothing was destroyed by this step."""


def check_roots(roots: Iterable[str]) -> list[str]:
    """Return ``roots`` in destroy order; refuse protected or unknown roots."""
    requested = list(dict.fromkeys(roots))
    protected = sorted(set(requested) & PROTECTED_ROOTS)
    if protected:
        raise TeardownRefused(f"never destroyed by destroy-dev: {', '.join(protected)}")
    unknown = sorted(set(requested) - set(ROOT_ORDER))
    if unknown:
        raise TeardownRefused(f"unknown root(s): {', '.join(unknown)}")
    return [r for r in ROOT_ORDER if r in requested]


def _without_instance_key(address: str) -> str:
    # module.a["k"].aws_x.y["z"] -> module.a.aws_x.y ; keys may contain dots, so strip [..] first.
    return re.sub(r"\[[^\]]*\]", "", address)


def is_kept(address: str) -> bool:
    bare = _without_instance_key(address)
    return any(p.search(bare) for p in KEEP_PATTERNS)


def managed_addresses(state_list: Iterable[str]) -> list[str]:
    """Managed resources from ``terraform state list`` (data sources are not destroyable)."""
    out = []
    for line in state_list:
        address = line.strip()
        if not address:
            continue
        last = _without_instance_key(address).split(".")
        # A data source address has "data" as the segment before its type.
        if len(last) >= 3 and last[-3] == "data":
            continue
        out.append(address)
    return out


@dataclass(frozen=True)
class DestroySelection:
    targets: list[str]
    kept: list[str]


def select_targets(addresses: Iterable[str]) -> DestroySelection:
    addresses = list(addresses)
    return DestroySelection(
        targets=[a for a in addresses if not is_kept(a)],
        kept=[a for a in addresses if is_kept(a)],
    )


@dataclass(frozen=True)
class PlanCheck:
    deletes: list[str]
    kept_untouched: list[str]


def verify_destroy_plan(plan: Mapping[str, Any], kept: Iterable[str]) -> PlanCheck:
    """Check ``terraform show -json`` of a destroy plan.

    Refuses when the plan deletes a kept resource (a kept resource depends on a destroyed one,
    so Terraform would take it too) or creates, updates or replaces anything.
    """
    kept = set(kept)
    deletes: list[str] = []
    problems: list[str] = []
    for change in plan.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        address = change["address"]
        actions = change["change"]["actions"]
        if actions == ["no-op"]:
            continue
        if actions != ["delete"]:
            problems.append(f"{address}: {'/'.join(actions)} (a destroy plan must only delete)")
            continue
        if address in kept or is_kept(address):
            problems.append(f"{address}: kept resource would be deleted")
            continue
        deletes.append(address)
    if problems:
        raise TeardownRefused("destroy plan rejected:\n  " + "\n  ".join(problems))
    return PlanCheck(deletes=sorted(deletes), kept_untouched=sorted(kept))


# --- AWS checks and backup (clients are passed in; tests use stubs) -------------------------


def active_work(glue: Any, sfn: Any, prefix: str = NAME_PREFIX) -> list[str]:
    """Glue job runs and Step Functions executions still in progress for ``prefix`` resources."""
    active: list[str] = []
    for page in glue.get_paginator("get_jobs").paginate():
        for job in page["Jobs"]:
            if not job["Name"].startswith(prefix):
                continue
            runs = glue.get_job_runs(JobName=job["Name"], MaxResults=10)["JobRuns"]
            active += [
                f"glue {job['Name']} {r['Id']} {r['JobRunState']}"
                for r in runs
                if r["JobRunState"] in ACTIVE_GLUE_STATES
            ]
    for page in sfn.get_paginator("list_state_machines").paginate():
        for machine in page["stateMachines"]:
            if not machine["name"].startswith(prefix):
                continue
            running = sfn.list_executions(
                stateMachineArn=machine["stateMachineArn"], statusFilter="RUNNING", maxResults=10
            )["executions"]
            active += [f"states {machine['name']} {e['name']} RUNNING" for e in running]
    return active


@dataclass(frozen=True)
class TableBackup:
    table: str
    items: int
    s3_uri: str


def backup_tables(
    dynamodb: Any, s3: Any, tables: Iterable[str], bucket: str, now: datetime
) -> list[TableBackup]:
    """Copy every item of each table to ``s3://bucket/teardown/<UTC time>/<table>.jsonl``.

    Items are written in DynamoDB's own attribute-value JSON, one per line, so a restore is a
    lossless ``put_item`` per line. The bucket's default SSE-KMS encrypts them.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    results = []
    for table in tables:
        lines = []
        for page in dynamodb.get_paginator("scan").paginate(TableName=table, ConsistentRead=True):
            lines += [json.dumps(item, sort_keys=True) for item in page["Items"]]
        key = f"teardown/{stamp}/{table}.jsonl"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=("\n".join(lines) + ("\n" if lines else "")).encode(),
            ContentType="application/x-ndjson",
        )
        results.append(TableBackup(table=table, items=len(lines), s3_uri=f"s3://{bucket}/{key}"))
    return results


CONFIRMATION = "destroy procurelake-dev"
