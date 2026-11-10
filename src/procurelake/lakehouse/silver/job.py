"""Run one PostgreSQL snapshot batch from Bronze into every Silver Iceberg table.

This is the logic behind the Glue job ``jobs/glue/bronze_to_silver.py``:

1. Load the ingestion manifests for every Silver source table and choose the batch: the one
   asked for, or the latest batch with a succeeded manifest for **every** table.
2. Run each table (``pipeline.run_table``). Tables are independent: a blocked table does not
   stop the others.
3. Write a run summary to the artifacts store (``pipeline_runs/silver/...``), then fail the job
   if any table was blocked, so the Glue run status reflects the quality gate.

Contracts and pipeline configs ship inside the wheel (``procurelake/_bundled/``, see
``pyproject.toml``), so the code and the rules it applies are always the same version.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from procurelake.contracts.source import load_contracts
from procurelake.ingestion.batch import code_version
from procurelake.ingestion.landing import LandingStore
from procurelake.ingestion.manifest import IngestionManifest, load_manifests
from procurelake.lakehouse.silver.pipeline import SilverTableRun, run_table, select_snapshot_batch
from procurelake.lakehouse.silver.spec import SilverTableSpec, load_specs

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "postgres"
RUNS_ROOT = "pipeline_runs/silver"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SilverQualityGateError(RuntimeError):
    """Raised after the run summary is written when one or more tables were blocked."""


def resource_root() -> Path:
    """Directory holding ``contracts/source`` and ``configs/pipelines/silver``.

    In a built wheel they are bundled under ``procurelake/_bundled``. In an editable checkout
    (local development, tests) they are read from the repository root.
    """
    bundled = Path(str(resources.files("procurelake"))) / "_bundled"
    if (bundled / "contracts" / "source").is_dir():
        return bundled
    repo = Path(__file__).resolve().parents[4]
    if (repo / "contracts" / "source").is_dir() and (repo / "pyproject.toml").is_file():
        return repo
    raise FileNotFoundError("contracts/source not found in the package or the repository")


def load_silver_specs(root: Path | None = None) -> dict[str, SilverTableSpec]:
    root = root or resource_root()
    return load_specs(
        load_contracts(root / "contracts" / "source"), root / "configs" / "pipelines" / "silver"
    )


def run_batch(
    spark,
    specs: dict[str, SilverTableSpec],
    artifacts: LandingStore,
    catalog: str,
    database: str,
    quarantine_root: str,
    location_root: str | None = None,
    batch_id: str | None = None,
    run_id: str | None = None,
    wheel_sha256: str | None = None,
) -> tuple[str, list[SilverTableRun], str]:
    """Apply one snapshot batch to every Silver table. Returns (batch_id, runs, summary_key)."""
    for name in (catalog, database):
        if not _NAME.match(name):
            raise ValueError(f"invalid catalog/database name {name!r}")
    started = datetime.now(UTC)
    manifests: list[IngestionManifest] = []
    for table in specs:
        manifests.extend(load_manifests(artifacts, SOURCE_SYSTEM, table))
    chosen = batch_id or select_snapshot_batch(manifests, specs)
    by_table = {
        m.dataset: m
        for m in manifests
        if m.batch_id == chosen and m.status == "succeeded" and m.window is None
    }
    missing = sorted(set(specs) - set(by_table))
    if missing:
        raise LookupError(f"batch {chosen} has no succeeded snapshot manifest for {missing}")

    runs: list[SilverTableRun] = []
    for table, spec in specs.items():
        fq_table = f"{catalog}.{database}.{spec.silver_table}"
        location = f"{location_root.rstrip('/')}/{spec.silver_table}" if location_root else None
        run = run_table(spark, spec, by_table[table], fq_table, quarantine_root, location)
        logger.info(
            "%s: %s (input=%d valid=%d rejected=%d)",
            fq_table,
            run.status,
            run.input_rows,
            run.valid_rows,
            run.rejected_rows,
        )
        runs.append(run)

    completed = datetime.now(UTC)
    run_id = run_id or completed.strftime("%Y%m%dT%H%M%SZ")
    summary = {
        "summary_format_version": "1",
        "batch_id": chosen,
        "run_id": run_id,
        "catalog": catalog,
        "database": database,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "code_version": code_version(),
        "wheel_sha256": wheel_sha256,
        "status": "blocked" if any(r.status == "blocked" for r in runs) else "succeeded",
        "tables": [asdict(r) for r in runs],
    }
    key = f"{RUNS_ROOT}/{SOURCE_SYSTEM}/batch_id={chosen}/run_id={run_id}/summary.json"
    artifacts.put_new(key, json.dumps(summary, indent=2, sort_keys=True).encode() + b"\n", "application/json")
    return chosen, runs, key


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts-bucket", required=True)
    parser.add_argument("--catalog", default="glue_catalog")
    parser.add_argument("--silver-database", required=True)
    parser.add_argument("--silver-location", required=True, help="s3://bucket/prefix for Silver tables")
    parser.add_argument("--quarantine-root", required=True, help="s3://bucket/quarantine")
    parser.add_argument("--batch-id", default="", help="Snapshot batch to apply (default: latest complete)")
    parser.add_argument("--region", default="ca-central-1")
    parser.add_argument("--wheel-sha256", default="", help="SHA-256 of the deployed wheel (provenance)")
    # Glue adds its own arguments (--JOB_NAME, --JOB_RUN_ID, ...); keep the run id, ignore the rest.
    parser.add_argument("--JOB_RUN_ID", dest="job_run_id", default="")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str]) -> None:
    """Glue entrypoint body. Builds the real S3 store and the job's Spark session.

    Returns normally on success and raises on failure: Glue treats any ``SystemExit`` as a failed
    run, so the entrypoint must not pass a return code to ``sys.exit``.
    """
    import boto3
    from pyspark.sql import SparkSession

    from procurelake.ingestion.landing import S3LandingStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    spark = SparkSession.builder.getOrCreate()
    artifacts = S3LandingStore(boto3.client("s3", region_name=args.region), args.artifacts_bucket)
    batch, runs, key = run_batch(
        spark,
        load_silver_specs(),
        artifacts,
        catalog=args.catalog,
        database=args.silver_database,
        quarantine_root=args.quarantine_root,
        location_root=args.silver_location,
        batch_id=args.batch_id or None,
        run_id=args.job_run_id or None,
        wheel_sha256=args.wheel_sha256 or None,
    )
    logger.info("batch %s: summary s3://%s/%s", batch, args.artifacts_bucket, key)
    blocked = [r.table for r in runs if r.status == "blocked"]
    if blocked:
        raise SilverQualityGateError(f"batch {batch}: quality gate blocked {blocked}; see {key}")
