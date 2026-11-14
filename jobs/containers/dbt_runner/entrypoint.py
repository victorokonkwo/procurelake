"""Run dbt in the dbt runner container and publish its artifacts (ADR 0029).

Configuration comes from the environment (task definition and Step Functions overrides):

  PROCURELAKE_DBT_COMMAND       dbt subcommand, default "build"
  PROCURELAKE_DBT_ARGS          extra dbt arguments, space-separated (e.g. "--select fct_invoice")
  PROCURELAKE_DBT_CHANGED_SILVER  CDC Gold build queue (ADR 0050): a JSON array of
                                "<silver table>#<batch id>" entries. Non-empty: build only the
                                nodes downstream of those tables of the dbt source "silver"
                                (source:silver.<table>+), with their tests. "all", empty or
                                unset: the whole project. Every table must be one of that
                                source's tables.
  PROCURELAKE_RUN_ID            run identifier (Step Functions execution name)
  PROCURELAKE_ARTIFACTS_BUCKET  bucket for dbt artifacts (dbt/runs/<run id>/)
  plus the profile variables read by dbt/profiles.yml.

The process exits with dbt's own exit code, after uploading run_results.json and manifest.json,
so the orchestrator sees the real outcome and the evidence survives a failed run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml  # PyYAML, a dbt-core dependency (dbt group in uv.lock)

PROJECT = Path("/app/dbt")
TARGET = Path("/tmp/target")  # noqa: S108 - single-user container; /tmp is its only writable area
ARTIFACTS = ("run_results.json", "manifest.json")
_ALLOWED_COMMANDS = {"build", "run", "test", "parse", "debug", "source"}
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# The CDC-fed Silver tables are the tables of this dbt source (ADR 0050).
CDC_SOURCE = "silver"
CDC_SOURCES_FILE = Path("models") / "staging" / "postgres" / "_postgres__sources.yml"
_QUEUE_ENTRY = re.compile(r"^(?P<table>[a-z][a-z0-9_]{0,127})#(?P<batch>[A-Za-z0-9_.:-]{1,128})$")
# More entries than this (failed builds piling up) means a full build: the selector would
# cover most of the project anyway, and the ECS overrides stay well under their size limit.
MAX_QUEUE_ENTRIES = 40
FULL_BUILD = "all"
_SELECTION_FLAGS = ("--select", "-s", "--selector", "--exclude", "--models", "-m", "--resource-type")


def cdc_source_tables(project: Path = PROJECT) -> frozenset[str]:
    """Table names of the dbt source ``silver``, read from its sources file in the image."""
    document = yaml.safe_load((project / CDC_SOURCES_FILE).read_text())
    for source in document.get("sources") or []:
        if source.get("name") == CDC_SOURCE:
            return frozenset(t["name"] for t in source.get("tables") or [])
    raise ValueError(f"dbt source {CDC_SOURCE!r} not found in {CDC_SOURCES_FILE}")


def changed_silver_tables(raw: str, allowed: frozenset[str]) -> list[str] | None:
    """Sorted Silver tables named by the queue entries in ``raw``; None means a full build.

    ``raw`` comes from the state machine (States.JsonToString of the DynamoDB string set). It is
    parsed as JSON, never passed to a shell, and every table must be in ``allowed``: anything
    else fails the run rather than building the wrong thing.
    """
    if raw.strip() in ("", FULL_BUILD):
        return None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PROCURELAKE_DBT_CHANGED_SILVER is not JSON") from exc
    if not isinstance(entries, list) or not entries or not all(isinstance(e, str) for e in entries):
        raise ValueError("PROCURELAKE_DBT_CHANGED_SILVER must be a non-empty JSON array of strings")
    if len(entries) > MAX_QUEUE_ENTRIES:
        return None
    tables = set()
    for entry in entries:
        match = _QUEUE_ENTRY.match(entry)
        if not match:
            raise ValueError(f"invalid queue entry {entry!r}")
        if match.group("table") not in allowed:
            raise ValueError(f"{match.group('table')!r} is not a table of the dbt source {CDC_SOURCE!r}")
        tables.add(match.group("table"))
    return sorted(tables)


def selection_args(tables: list[str]) -> list[str]:
    """``--select source:silver.<table>+ ...``: each table's downstream nodes (and their tests)."""
    return ["--select", *(f"source:{CDC_SOURCE}.{t}+" for t in tables)]


def dbt_command(env: dict[str, str], project: Path = PROJECT) -> list[str]:
    command = env.get("PROCURELAKE_DBT_COMMAND", "build")
    if command not in _ALLOWED_COMMANDS:
        raise ValueError(f"unsupported dbt command {command!r}")
    extra = env.get("PROCURELAKE_DBT_ARGS", "").split()
    changed = env.get("PROCURELAKE_DBT_CHANGED_SILVER", "")
    if changed.strip() not in ("", FULL_BUILD):
        if command != "build":
            raise ValueError("PROCURELAKE_DBT_CHANGED_SILVER applies to dbt build only")
        if any(a.split("=", 1)[0] in _SELECTION_FLAGS for a in extra):
            raise ValueError(
                "PROCURELAKE_DBT_CHANGED_SILVER cannot be combined with a selection in PROCURELAKE_DBT_ARGS"
            )
        tables = changed_silver_tables(changed, cdc_source_tables(project))
        if tables is not None:
            extra = [*selection_args(tables), *extra]
    # A full refresh drops and recreates the Gold Iceberg tables, and Lake Formation deletes a
    # dropped table's tenant filters, grants and opt-ins with it (live, 2026-11-10; ADR 0030).
    # It is an operator action followed by re-granting, never a scheduled run.
    if env.get("DBT_FULL_REFRESH", "").strip().lower() not in ("", "0", "false"):
        raise ValueError("DBT_FULL_REFRESH is not allowed in the scheduled runner")
    for arg in extra:
        short_flags = arg.startswith("-") and not arg.startswith("--")
        if arg.startswith("--full-refresh") or (short_flags and "f" in arg[1:]):
            raise ValueError(f"full refresh is not allowed in the scheduled runner: {arg!r}")
    return [
        "dbt", command,
        "--project-dir", str(project), "--profiles-dir", str(project),
        "--target-path", str(TARGET),
        *extra,
    ]  # fmt: skip


def upload_artifacts(s3, bucket: str, run_id: str, target: Path = TARGET) -> list[str]:
    if not _RUN_ID.match(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    keys = []
    for name in ARTIFACTS:
        path = target / name
        if path.is_file():
            key = f"dbt/runs/{run_id}/{name}"
            s3.upload_file(str(path), bucket, key)
            keys.append(key)
    return keys


def main() -> int:
    env = dict(os.environ)
    command = dbt_command(env)
    print("running:", " ".join(command), flush=True)
    code = subprocess.run(command, check=False).returncode  # noqa: S603 - argv built from an allow-list
    bucket, run_id = env.get("PROCURELAKE_ARTIFACTS_BUCKET"), env.get("PROCURELAKE_RUN_ID", "")
    if bucket and run_id:
        import boto3

        keys = upload_artifacts(boto3.client("s3"), bucket, run_id)
        print(f"uploaded {keys} to s3://{bucket}/", flush=True)
    print(f"dbt exit code {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
