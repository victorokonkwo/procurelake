"""Command-line entrypoint for source ingestion.

Examples:
    # One specific day into the dev lakehouse, as the ingestion role:
    python -m procurelake.ingestion.cli usaspending --date 2026-11-08 \\
        --raw-bucket <raw bucket> --manifest-bucket <artifacts bucket> --role-arn <ingestion role>

    # Incremental: every day since --since without a succeeded batch (max 3 per run):
    python -m procurelake.ingestion.cli usaspending --since 2026-11-01 --max-days 3 ...

    # Local filesystem instead of S3 (no AWS needed):
    python -m procurelake.ingestion.cli usaspending --date 2026-11-08 --local-root ./local_lake

The exit code is non-zero if any batch fails reconciliation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.ingestion.connectors.usaspending import UsaspendingConnector, plan_incremental
from procurelake.ingestion.http import HttpClient
from procurelake.ingestion.landing import LandingStore, LocalLandingStore, S3LandingStore


def _stores(args: argparse.Namespace) -> tuple[LandingStore, LandingStore]:
    if args.local_root:
        root = Path(args.local_root)
        return LocalLandingStore(root / "raw"), LocalLandingStore(root / "artifacts")
    if not (args.raw_bucket and args.manifest_bucket):
        raise SystemExit("--raw-bucket and --manifest-bucket are required unless --local-root is given")
    session = build_session(region=args.region, role_arn=args.role_arn, session_name="procurelake-ingestion")
    s3 = session.client("s3")
    return S3LandingStore(s3, args.raw_bucket), S3LandingStore(s3, args.manifest_bucket)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="source", required=True)
    usa = sub.add_parser("usaspending", help="USAspending.gov contract awards by last_modified_date")
    when = usa.add_mutually_exclusive_group(required=True)
    when.add_argument("--date", type=date.fromisoformat, help="single UTC day (YYYY-MM-DD)")
    when.add_argument("--since", type=date.fromisoformat, help="fill every missing day since this date")
    usa.add_argument("--max-days", type=int, default=None, help="cap on days per incremental run")
    usa.add_argument("--raw-bucket")
    usa.add_argument("--manifest-bucket")
    usa.add_argument("--role-arn", help="IAM role to assume (e.g. procurelake-dev-ingestion)")
    usa.add_argument("--region", default=DEFAULT_REGION)
    usa.add_argument("--local-root", help="write to this directory instead of S3")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raw_store, manifest_store = _stores(args)
    connector = UsaspendingConnector(HttpClient(), raw_store, manifest_store)

    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    days = (
        [args.date] if args.date else plan_incremental(manifest_store, args.since, yesterday, args.max_days)
    )
    if not days:
        print("Nothing to ingest: every day in range already has a succeeded batch.")
        return 0

    failures = 0
    for day in days:
        manifest = connector.ingest_day(day)
        rec = manifest.reconciliation
        print(
            f"{day} batch={manifest.batch_id} status={manifest.status} files={len(manifest.files)} "
            f"records={rec.landed_record_count if rec else 0} "
            f"expected={rec.expected_count_before if rec else None} "
            f"detail={rec.detail if rec else manifest.error}"
        )
        failures += manifest.status != "succeeded"
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
