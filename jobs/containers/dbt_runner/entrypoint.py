"""Run dbt in the dbt runner container and publish its artifacts (ADR 0029).

Configuration comes from the environment (task definition and Step Functions overrides):

  PROCURELAKE_DBT_COMMAND       dbt subcommand, default "build"
  PROCURELAKE_DBT_ARGS          extra dbt arguments, space-separated (e.g. "--select fct_invoice")
  PROCURELAKE_RUN_ID            run identifier (Step Functions execution name)
  PROCURELAKE_ARTIFACTS_BUCKET  bucket for dbt artifacts (dbt/runs/<run id>/)
  plus the profile variables read by dbt/profiles.yml.

The process exits with dbt's own exit code, after uploading run_results.json and manifest.json,
so the orchestrator sees the real outcome and the evidence survives a failed run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path("/app/dbt")
TARGET = Path("/tmp/target")  # noqa: S108 - single-user container; /tmp is its only writable area
ARTIFACTS = ("run_results.json", "manifest.json")
_ALLOWED_COMMANDS = {"build", "run", "test", "parse", "debug", "source"}
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def dbt_command(env: dict[str, str]) -> list[str]:
    command = env.get("PROCURELAKE_DBT_COMMAND", "build")
    if command not in _ALLOWED_COMMANDS:
        raise ValueError(f"unsupported dbt command {command!r}")
    extra = env.get("PROCURELAKE_DBT_ARGS", "").split()
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
        "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT),
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
