"""Run the reference-data Silver job (USAspending; ADR 0044) and record what it applied.

This is the logic behind the Glue job ``jobs/glue/reference_to_silver.py``:

1. Load the contract (bundled in the wheel) and every manifest of its dataset.
2. Applied batches = the union of ``applied_batch_ids`` in earlier run summaries under
   ``pipeline_runs/silver/<source_system>/reference/``. Pending = succeeded, settled, not applied,
   oldest first, at most ``--max-batches`` per run (the rest wait for the next run).
3. ``reference.apply_batches``: validate, quarantine, deduplicate, MERGE, validate.
4. Write the run summary (the checkpoint) only after that succeeded; then fail the run if any
   batch was blocked by the quality gate.

A run with nothing pending writes a summary with no batches, so every scheduled run leaves a
record.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import UTC, datetime

from procurelake.contracts.reference import ReferenceContract
from procurelake.ingestion.batch import code_version, new_batch_id
from procurelake.ingestion.landing import LandingStore
from procurelake.ingestion.manifest import load_manifests

logger = logging.getLogger(__name__)

RUNS_ROOT = "pipeline_runs/silver"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReferenceQualityGateError(RuntimeError):
    """Raised after the run summary is written when one or more batches were blocked."""


def load_contract(name: str) -> ReferenceContract:
    from procurelake.lakehouse.silver.resources import load_reference

    contracts = load_reference()
    if name not in contracts:
        raise LookupError(f"no reference contract named {name!r} (have {sorted(contracts)})")
    return contracts[name]


def summaries_prefix(contract: ReferenceContract) -> str:
    return f"{RUNS_ROOT}/{contract.source_system}/reference/{contract.name}/"


def applied_batch_ids(artifacts: LandingStore, contract: ReferenceContract) -> set[str]:
    applied: set[str] = set()
    for key in artifacts.list_keys(summaries_prefix(contract)):
        if key.endswith("/summary.json"):
            applied.update(json.loads(artifacts.get(key))["applied_batch_ids"])
    return applied


def run(
    spark,
    contract: ReferenceContract,
    artifacts: LandingStore,
    catalog: str,
    database: str,
    quarantine_root: str,
    location_root: str | None = None,
    max_batches: int = 60,
    run_id: str | None = None,
    wheel_sha256: str | None = None,
) -> tuple[dict, str]:
    """Apply pending batches; returns (summary, summary key)."""
    from procurelake.lakehouse.silver.reference import ReferenceRun, apply_batches, pending_batches

    for name in (catalog, database):
        if not _NAME.match(name):
            raise ValueError(f"invalid catalog/database name {name!r}")
    if max_batches < 1:
        raise ValueError("max_batches must be >= 1")
    started = datetime.now(UTC)
    fq_table = f"{catalog}.{database}.{contract.silver_table}"
    manifests = load_manifests(artifacts, contract.source_system, contract.dataset)
    applied = applied_batch_ids(artifacts, contract)
    pending = pending_batches(manifests, contract, applied)
    todo = pending[:max_batches]
    location = f"{location_root.rstrip('/')}/{contract.silver_table}" if location_root else None
    result = (
        apply_batches(spark, contract, todo, fq_table, quarantine_root, location)
        if todo
        else ReferenceRun(fq_table)
    )
    completed = datetime.now(UTC)
    # Unique even for two runs in the same second (manual reruns): the batch-id format.
    run_id = run_id or new_batch_id(completed)
    summary = {
        "summary_format_version": "1",
        "contract": contract.name,
        "contract_version": contract.contract_version,
        "source_system": contract.source_system,
        "run_id": run_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "code_version": code_version(),
        "wheel_sha256": wheel_sha256,
        "pending_before_run": len(pending),
        "left_for_next_run": len(pending) - len(todo),
        "status": "blocked" if result.blocked else "succeeded",
        **result.as_dict(),
    }
    key = f"{summaries_prefix(contract)}run_date={completed.date().isoformat()}/run_id={run_id}/summary.json"
    artifacts.put_new(key, json.dumps(summary, indent=2, sort_keys=True).encode() + b"\n", "application/json")
    return summary, key


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", default="usaspending_contract_awards")
    parser.add_argument("--artifacts-bucket", required=True)
    parser.add_argument("--catalog", default="glue_catalog")
    parser.add_argument("--silver-database", required=True)
    parser.add_argument("--silver-location", required=True, help="s3://bucket/prefix for Silver tables")
    parser.add_argument("--quarantine-root", required=True, help="s3://bucket/quarantine")
    parser.add_argument("--max-batches", type=int, default=60)
    parser.add_argument("--region", default="ca-central-1")
    parser.add_argument("--wheel-sha256", default="")
    parser.add_argument("--JOB_RUN_ID", dest="job_run_id", default="")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str]) -> None:
    """Glue entrypoint body. Raises on failure (never ``sys.exit``; see jobs/glue)."""
    import boto3
    from pyspark.sql import SparkSession

    from procurelake.ingestion.landing import S3LandingStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    spark = SparkSession.builder.getOrCreate()
    artifacts = S3LandingStore(boto3.client("s3", region_name=args.region), args.artifacts_bucket)
    summary, key = run(
        spark,
        load_contract(args.contract),
        artifacts,
        catalog=args.catalog,
        database=args.silver_database,
        quarantine_root=args.quarantine_root,
        location_root=args.silver_location,
        max_batches=args.max_batches,
        run_id=args.job_run_id or None,
        wheel_sha256=args.wheel_sha256 or None,
    )
    for b in summary["batches"]:
        logger.info(
            "batch %s (%s, %s): %s new=%d changed=%d unchanged=%d older=%d rejected=%d",
            b["batch_id"], b["window_date"], b["purpose"], b["status"], b["new_keys"], b["changed_keys"],
            b["unchanged_keys"], b["older_than_silver"], b["rejected_records"],
        )  # fmt: skip
    logger.info("summary s3://%s/%s", args.artifacts_bucket, key)
    if summary["blocked_batch_ids"]:
        raise ReferenceQualityGateError(f"quality gate blocked {summary['blocked_batch_ids']}; see {key}")
