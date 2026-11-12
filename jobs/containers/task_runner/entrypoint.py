"""Run one allow-listed task in the task runner container (ADR 0040).

Configuration comes from the environment (task definition and Step Functions overrides):

  PROCURELAKE_TASK              ingest-usaspending | ingest-canada-contracts | data-quality
  PROCURELAKE_RUN_ID            run identifier (Step Functions execution name)
  PROCURELAKE_RAW_BUCKET, PROCURELAKE_ARTIFACTS_BUCKET
  PROCURELAKE_USASPENDING_SINCE first day the USAspending gap-filler covers (YYYY-MM-DD)
  PROCURELAKE_USASPENDING_MAX_DAYS  cap on days per run
  PROCURELAKE_ATHENA_WORKGROUP, PROCURELAKE_SILVER_DATABASE  (data-quality)

Only these tasks run, with arguments built here: nothing from the input reaches a command line
except validated dates, counts and the run id. The process exits with the task's exit code.
The task's AWS rights are its task role's: the ingestion role for ingestion, the data engineer
for the data-quality checks (set per run by the state machine).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TASKS = ("ingest-usaspending", "ingest-canada-contracts", "data-quality")


def _need(env: dict[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def task_argv(env: dict[str, str]) -> tuple[str, list[str]]:
    """(python module, argv) for the configured task."""
    task = env.get("PROCURELAKE_TASK", "")
    if task not in TASKS:
        raise ValueError(f"unsupported task {task!r}; expected one of {TASKS}")
    run_id = _need(env, "PROCURELAKE_RUN_ID")
    if not _RUN_ID.match(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    if task == "data-quality":
        return "procurelake.quality.cli", [
            "--artifacts-bucket", _need(env, "PROCURELAKE_ARTIFACTS_BUCKET"),
            "--workgroup", _need(env, "PROCURELAKE_ATHENA_WORKGROUP"),
            "--silver-database", _need(env, "PROCURELAKE_SILVER_DATABASE"),
            "--run-id", run_id,
        ]  # fmt: skip
    stores = [
        "--raw-bucket", _need(env, "PROCURELAKE_RAW_BUCKET"),
        "--manifest-bucket", _need(env, "PROCURELAKE_ARTIFACTS_BUCKET"),
    ]  # fmt: skip
    if task == "ingest-canada-contracts":
        return "procurelake.ingestion.cli", ["canada-contracts", "--skip-if-unchanged", *stores]
    since = date.fromisoformat(_need(env, "PROCURELAKE_USASPENDING_SINCE")).isoformat()
    max_days = int(env.get("PROCURELAKE_USASPENDING_MAX_DAYS", "3"))
    if not 1 <= max_days <= 31:
        raise ValueError("PROCURELAKE_USASPENDING_MAX_DAYS must be between 1 and 31")
    return "procurelake.ingestion.cli", [
        "usaspending",
        "--since",
        since,
        "--max-days",
        str(max_days),
        *stores,
    ]


def main() -> int:
    module, argv = task_argv(dict(os.environ))
    print(f"running: python -m {module} {' '.join(argv)}", flush=True)
    import runpy

    sys.argv = [module, *argv]
    try:
        runpy.run_module(module, run_name="__main__")
    except SystemExit as exc:  # the CLIs end with sys.exit(code)
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        print(f"exit code {code}", flush=True)
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
