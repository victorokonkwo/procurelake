"""Glue job body for CDC apply (``jobs/glue/apply_cdc.py``; ADR 0028).

For each Silver table: take its lease, apply new Bronze DMS files in order, advance the
DynamoDB checkpoint, release the lease (``job.run_cdc_table``). Tables are independent; the job
fails at the end if any table was blocked or errored, after attempting all of them.

Full-load completeness (needed to tombstone keys missing from a re-seeded source) comes from
AWS DMS when a task ARN is given: a table's full load under the task's current ``load_id``
is complete when DMS reports ``TableState = "Table completed"`` for it. Until then that load is
**deferred** for the table (none of its files are applied), so a scheduled run that starts
during the full load cannot record it as applied without the tombstoning. Without a task ARN
(the disposable CDC stack is down), no full load is treated as complete.
"""

from __future__ import annotations

import argparse
import logging
import re

from procurelake.cdc.apply import load_id_of
from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.cdc.job import run_cdc_table

logger = logging.getLogger(__name__)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CdcJobError(RuntimeError):
    """Raised after all tables were attempted, if any was blocked or failed."""


def completed_full_loads(dms, task_arn: str, schema: str = "procurement") -> dict[str, str]:
    """{table: load_id} for tables whose full load DMS reports as completed for ``task_arn``."""
    load_id, done = dms_load_status(dms, task_arn, schema)
    return {table: load_id for table in done}


def dms_load_status(dms, task_arn: str, schema: str = "procurement") -> tuple[str, set[str]]:
    """(the task's current load_id, tables whose full load DMS reports as completed)."""
    task = dms.describe_replication_tasks(Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}])[
        "ReplicationTasks"
    ][0]
    endpoint = dms.describe_endpoints(
        Filters=[{"Name": "endpoint-arn", "Values": [task["TargetEndpointArn"]]}]
    )["Endpoints"][0]
    load_id = load_id_of(f"/{endpoint['S3Settings']['BucketFolder'].strip('/')}/")
    done: set[str] = set()
    paginator = dms.get_paginator("describe_table_statistics")
    for page in paginator.paginate(ReplicationTaskArn=task_arn):
        for stats in page["TableStatistics"]:
            if stats["SchemaName"] == schema and stats["TableState"] == "Table completed":
                done.add(stats["TableName"])
    return load_id, done


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-bucket", required=True)
    parser.add_argument("--cdc-root", default="bronze/postgres_cdc")
    parser.add_argument("--checkpoint-table", required=True)
    parser.add_argument("--catalog", default="glue_catalog")
    parser.add_argument("--silver-database", required=True)
    parser.add_argument("--silver-location", required=True)
    parser.add_argument("--quarantine-root", required=True)
    parser.add_argument("--tables", default="", help="Comma-separated source tables (default: all)")
    parser.add_argument("--dms-task-arn", default="", help="For full-load completeness (optional)")
    parser.add_argument("--region", default="ca-central-1")
    parser.add_argument("--wheel-sha256", default="")
    parser.add_argument("--JOB_RUN_ID", dest="job_run_id", default="")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str]) -> None:
    """Glue entrypoint body. Raises on failure (never sys.exit: ADR 0025)."""
    import boto3
    from pyspark.sql import SparkSession

    from procurelake.ingestion.landing import S3LandingStore
    from procurelake.lakehouse.silver.job import load_silver_specs

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    for name in (args.catalog, args.silver_database):
        if not _NAME.match(name):
            raise ValueError(f"invalid catalog/database name {name!r}")
    specs = load_silver_specs()
    wanted = [t for t in args.tables.split(",") if t] or list(specs)
    unknown = sorted(set(wanted) - set(specs))
    if unknown:
        raise ValueError(f"unknown tables {unknown}")

    spark = SparkSession.builder.getOrCreate()
    store = S3LandingStore(boto3.client("s3", region_name=args.region), args.raw_bucket)
    checkpoints = CheckpointStore(boto3.client("dynamodb", region_name=args.region), args.checkpoint_table)
    # With a DMS task: its current load counts as complete for tables DMS reports done, and is
    # deferred (not applied at all yet) for the others. Without one, nothing is complete.
    current_load, done = (
        dms_load_status(boto3.client("dms", region_name=args.region), args.dms_task_arn)
        if args.dms_task_arn
        else (None, set())
    )
    owner = f"glue:{args.job_run_id or 'manual'}"
    problems: list[str] = []
    for table in wanted:
        spec = specs[table]
        fq = f"{args.catalog}.{args.silver_database}.{spec.silver_table}"
        try:
            results = run_cdc_table(
                spark,
                spec,
                store,
                args.cdc_root,
                checkpoints,
                fq,
                args.quarantine_root,
                owner,
                table_location=f"{args.silver_location.rstrip('/')}/{spec.silver_table}",
                complete_full_loads=frozenset({current_load}) if table in done else frozenset(),
                deferred_load_ids=(
                    frozenset({current_load}) if current_load and table not in done else frozenset()
                ),
            )
        except Exception as exc:  # attempt every table, then fail the run
            logger.exception("%s: CDC apply failed", fq)
            problems.append(f"{table}: {exc}")
            continue
        for r in results:
            logger.info(
                "%s %s: %s files=%d events=%d rejected=%d held=%d applied_keys=%d full_load_tombstones=%d",
                fq, r.batch_id, r.status, r.files, r.events, r.rejected_events, r.held_keys,
                r.applied_keys, r.full_load_tombstones,
            )  # fmt: skip
            if r.status == "blocked":
                problems.append(
                    f"{table}: batch {r.batch_id} blocked by the quality gate ({r.quarantine_uri})"
                )
    if problems:
        raise CdcJobError("; ".join(problems))
