"""Run one allow-listed task in the task runner container (ADR 0040).

Configuration comes from the environment (task definition and Step Functions overrides):

  PROCURELAKE_TASK              ingest-usaspending | resweep-usaspending | ingest-canada-contracts
                                | data-quality | extract-document
  PROCURELAKE_RUN_ID            run identifier (Step Functions execution name)
  PROCURELAKE_RAW_BUCKET, PROCURELAKE_ARTIFACTS_BUCKET
  PROCURELAKE_USASPENDING_SINCE first day the USAspending gap-filler covers (YYYY-MM-DD)
  PROCURELAKE_USASPENDING_MAX_DAYS  cap on days per run
  PROCURELAKE_USASPENDING_RESWEEP_DAYS  most recent settled days a re-sweep collects again (ADR 0044)
  PROCURELAKE_ATHENA_WORKGROUP, PROCURELAKE_SILVER_DATABASE  (data-quality)
  PROCURELAKE_AI_REVIEW_TABLE   review queue (extract-document)
  PROCURELAKE_DOCUMENT_MANIFEST_KEY  landing manifest that triggered the run (extract-document), or
  PROCURELAKE_DOCUMENT_ID       a document id, for a run started by hand (ADR 0045)

Only these tasks run, with arguments built here: nothing from the input reaches a command line
except validated dates, counts and the run id. The process exits with the task's exit code.
The task's AWS rights are its task role's: the ingestion role for ingestion, the data engineer
for the data-quality checks, the AI extraction role for extract-document (set per run by the
state machine).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TASKS = (
    "ingest-usaspending",
    "resweep-usaspending",
    "ingest-canada-contracts",
    "data-quality",
    "extract-document",
)
# Document ids as the catalog allows them (procurelake.ai.catalog): lower-case words joined by '-'.
_DOCUMENT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# The landing manifest of one document (procurelake.ai.documents): batch id = <document id>-<sha256[:16]>.
_DOCUMENT_MANIFEST = re.compile(
    r"^pipeline_manifests/public_contracts/contract_documents/window_start=snapshot/"
    r"batch_id=(?P<doc>[a-z0-9]+(?:-[a-z0-9]+)*)-[0-9a-f]{16}/manifest\.json$"
)


def document_id(env: dict[str, str]) -> str:
    """The document to extract: parsed from the triggering manifest key, or given by hand."""
    key = env.get("PROCURELAKE_DOCUMENT_MANIFEST_KEY", "")
    if key:
        match = _DOCUMENT_MANIFEST.match(key)
        if not match:
            raise ValueError(f"not a document landing manifest: {key!r}")
        return match["doc"]
    doc = _need(env, "PROCURELAKE_DOCUMENT_ID")
    if not _DOCUMENT_ID.match(doc):
        raise ValueError(f"invalid document id {doc!r}")
    return doc


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
    if task == "extract-document":
        return "procurelake.ai.cli", [
            "extract",
            "--raw-bucket", _need(env, "PROCURELAKE_RAW_BUCKET"),
            "--artifacts-bucket", _need(env, "PROCURELAKE_ARTIFACTS_BUCKET"),
            "--review-table", _need(env, "PROCURELAKE_AI_REVIEW_TABLE"),
            "--document-id", document_id(env),
            "--skip-extracted",
        ]  # fmt: skip
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
    if task == "resweep-usaspending":
        days = int(env.get("PROCURELAKE_USASPENDING_RESWEEP_DAYS", "30"))
        if not 1 <= days <= 60:
            raise ValueError("PROCURELAKE_USASPENDING_RESWEEP_DAYS must be between 1 and 60")
        return "procurelake.ingestion.cli", ["usaspending", "--resweep-days", str(days), *stores]
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
