"""Run the daily data-quality checks and write their report (ADR 0040).

    python -m procurelake.quality.cli --artifacts-bucket <b> --workgroup <wg> \\
        --silver-database <db> --run-id <id> [--source-system postgres] [--role-arn <arn>]

Writes ``pipeline_runs/quality/run_date=<UTC date>/run_id=<id>/report.json`` to the artifacts
bucket, prints a summary, and exits 1 when any check fails.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.ingestion.landing import S3LandingStore
from procurelake.lakehouse.silver.resources import load_silver_specs
from procurelake.quality.checks import (
    REPORTS_ROOT,
    Check,
    integrity_checks,
    latest_silver_summary,
    manifests_by_batch,
    report,
    summary_checks,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def quarantine_line_counter(s3):
    def count(uri: str) -> int:
        parsed = urlparse(uri)
        total = 0
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=parsed.netloc, Prefix=parsed.path.lstrip("/")
        ):
            for obj in page.get("Contents", []):
                if obj["Key"].rsplit("/", 1)[-1].startswith(("_", ".")):
                    continue  # _SUCCESS and checksum side files
                body = s3.get_object(Bucket=parsed.netloc, Key=obj["Key"])["Body"].read()
                total += sum(1 for line in body.splitlines() if line.strip())
        return total

    return count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--artifacts-bucket", required=True)
    p.add_argument("--workgroup", required=True)
    p.add_argument("--silver-database", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--source-system", default="postgres")
    p.add_argument("--role-arn")
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)
    if not _RUN_ID.match(args.run_id):
        raise SystemExit(f"invalid run id {args.run_id!r}")

    session = build_session(region=args.region, role_arn=args.role_arn, session_name="data-quality")
    s3 = session.client("s3")
    artifacts = S3LandingStore(s3, args.artifacts_bucket)
    specs = load_silver_specs()
    started = datetime.now(UTC)

    checks: list[Check] = []
    latest = latest_silver_summary(artifacts, args.source_system)
    summary_key = latest[0] if latest else None
    if latest is None:
        checks.append(Check("silver_run_exists", args.source_system, False, "no Silver run summary found"))
    else:
        manifests = manifests_by_batch(artifacts, args.source_system, specs)
        checks += summary_checks(latest[1], manifests, quarantine_line_counter(s3), args.source_system)
    checks += integrity_checks(session.client("athena"), args.workgroup, args.silver_database, specs.values())

    body = report(
        checks,
        run_id=args.run_id,
        started_at=started.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        source_system=args.source_system,
        silver_database=args.silver_database,
        silver_summary=summary_key,
    )
    key = f"{REPORTS_ROOT}/run_date={started.date().isoformat()}/run_id={args.run_id}/report.json"
    artifacts.put_new(key, json.dumps(body, indent=2).encode() + b"\n", "application/json")
    print(
        f"{body['checks_total']} checks, {body['checks_failed']} failed; report s3://{args.artifacts_bucket}/{key}"
    )
    for c in body["failed"]:
        print(f"FAILED {c['name']} {c['subject']}: {c['detail']}")
    return 0 if body["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
