"""Glue job body for daily Iceberg maintenance (ADR 0033): ``jobs/glue/iceberg_maintenance.py``.

Arguments (set by Terraform, ``infrastructure/live/dev/data_platform``):

* ``--silver-tables`` / ``--gold-tables``: comma-separated table names; ``--leased-tables``:
  the Silver tables the CDC apply job writes (maintained only under its DynamoDB lease);
* ``--silver-max-age-seconds`` / ``--silver-min-snapshots`` and the ``--gold-*`` pair: retention;
* ``--report-bucket`` / ``--report-prefix``: where the JSON run report is written.

The run fails (raises) if any table failed; a table skipped because the CDC writer held its
lease is not a failure (the next run maintains it).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

import boto3

from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.lakehouse.maintenance import Athena, Retention, TableResult, TableTarget, run

logger = logging.getLogger(__name__)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in (
        "--workgroup",
        "--silver-database",
        "--gold-database",
        "--report-bucket",
        "--checkpoint-table",
    ):
        p.add_argument(name, required=True)
    p.add_argument("--silver-tables", default="")
    p.add_argument("--gold-tables", default="")
    p.add_argument("--leased-tables", default="")
    p.add_argument("--silver-max-age-seconds", type=int, required=True)
    p.add_argument("--silver-min-snapshots", type=int, required=True)
    p.add_argument("--gold-max-age-seconds", type=int, required=True)
    p.add_argument("--gold-min-snapshots", type=int, required=True)
    p.add_argument("--report-prefix", default="iceberg_maintenance")
    p.add_argument("--region", default="ca-central-1")
    p.add_argument("--JOB_RUN_ID", dest="job_run_id", default="")
    args, _unknown = p.parse_known_args(argv)
    return args


def _names(csv: str) -> list[str]:
    return [n.strip() for n in csv.split(",") if n.strip()]


def targets(args: argparse.Namespace) -> list[TableTarget]:
    silver = Retention(args.silver_max_age_seconds, args.silver_min_snapshots)
    gold = Retention(args.gold_max_age_seconds, args.gold_min_snapshots)
    leased = set(_names(args.leased_tables))
    unknown = leased - set(_names(args.silver_tables))
    if unknown:
        raise ValueError(f"leased tables must be Silver tables: {sorted(unknown)}")
    return [TableTarget(args.silver_database, t, silver, t in leased) for t in _names(args.silver_tables)] + [
        TableTarget(args.gold_database, t, gold, False) for t in _names(args.gold_tables)
    ]


def report(results: list[TableResult], started: datetime, run_id: str) -> dict:
    def table(r: TableResult) -> dict:
        out = {"table": r.target.fq, "status": r.status, "error": r.error, "statements": r.statements}
        for label, h in (("before", r.before), ("after", r.after)):
            out[label] = None if h is None else {**asdict(h), "live_records": h.live_records}
        return out

    return {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "tables": [table(r) for r in results],
        "failed": [r.target.fq for r in results if r.status == "failed"],
        "skipped": [r.target.fq for r in results if r.status == "skipped_lease_held"],
    }


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    started = datetime.now(UTC)
    run_id = args.job_run_id or f"manual-{started:%Y%m%dT%H%M%SZ}"
    athena = Athena(boto3.client("athena", region_name=args.region), args.workgroup)
    checkpoints = CheckpointStore(boto3.client("dynamodb", region_name=args.region), args.checkpoint_table)
    results = run(athena, targets(args), checkpoints, owner=f"maintenance:{run_id}")
    body = report(results, started, run_id)
    key = f"{args.report_prefix}/run_date={started:%Y-%m-%d}/{run_id}.json"
    boto3.client("s3", region_name=args.region).put_object(
        Bucket=args.report_bucket,
        Key=key,
        Body=json.dumps(body, indent=2).encode(),
        ContentType="application/json",
    )
    for t in body["tables"]:
        logger.info("%s: %s before=%s after=%s", t["table"], t["status"], t["before"], t["after"])
    if body["failed"]:
        raise RuntimeError(
            f"maintenance failed for {body['failed']} (report s3://{args.report_bucket}/{key})"
        )
