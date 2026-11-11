"""Glue job body for partition evolution (ADR 0034): ``jobs/glue/evolve_partitions.py``.

Arguments:

* ``--catalog`` / ``--database``: the Iceberg catalog and Glue database (dev: the bench Silver
  database only; the job role cannot write any other);
* ``--fields``: ``table=transform(column)`` items, comma-separated
  (e.g. ``silver_invoices=months(invoice_date),silver_awards=months(awarded_at)``);
* ``--rewrite``: ``true`` to rewrite every data file into the current spec after adding the field;
* ``--report-bucket`` / ``--report-prefix``: where the JSON report is written.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from procurelake.lakehouse.partitioning import evolve, parse_fields

logger = logging.getLogger(__name__)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--catalog", default="glue_catalog")
    p.add_argument("--database", required=True)
    p.add_argument("--fields", required=True)
    p.add_argument("--rewrite", choices=("true", "false"), default="false")
    p.add_argument("--report-bucket", required=True)
    p.add_argument("--report-prefix", default="benchmarks/partition_evolution")
    p.add_argument("--region", default="ca-central-1")
    p.add_argument("--JOB_RUN_ID", dest="job_run_id", default="")
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv: list[str]) -> None:
    import boto3
    from pyspark.sql import SparkSession

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    fields = parse_fields(args.fields)  # fail on bad input before starting Spark work
    started = datetime.now(UTC)
    run_id = args.job_run_id or f"manual-{started:%Y%m%dT%H%M%SZ}"
    spark = SparkSession.builder.getOrCreate()
    results = [evolve(spark, args.catalog, args.database, f, args.rewrite == "true") for f in fields]
    body = {
        "run_id": run_id,
        "database": args.database,
        "rewrite": args.rewrite == "true",
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "tables": [asdict(r) for r in results],
    }
    key = f"{args.report_prefix}/{run_id}.json"
    boto3.client("s3", region_name=args.region).put_object(
        Bucket=args.report_bucket,
        Key=key,
        Body=json.dumps(body, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    logger.info("report s3://%s/%s", args.report_bucket, key)
