"""Command-line entrypoint for source ingestion.

Examples:
    # One specific day into the dev lakehouse, as the ingestion role:
    python -m procurelake.ingestion.cli usaspending --date 2026-11-08 \\
        --raw-bucket <raw bucket> --manifest-bucket <artifacts bucket> --role-arn <ingestion role>

    # Incremental: every day since --since without a succeeded batch (max 3 per run):
    python -m procurelake.ingestion.cli usaspending --since 2026-11-01 --max-days 3 ...

    # Weekly re-sweep: collect the 30 most recent settled days again (ADR 0044):
    python -m procurelake.ingestion.cli usaspending --resweep-days 30 ...

    # Local filesystem instead of S3 (no AWS needed):
    python -m procurelake.ingestion.cli usaspending --date 2026-11-08 --local-root ./local_lake

    # Government of Canada contracts over $10,000 (downloaded, or from a pinned local copy):
    python -m procurelake.ingestion.cli canada-contracts --local-root ./local_lake
    python -m procurelake.ingestion.cli canada-contracts --path contracts.csv --expect-sha256 <sha> ...

    # Any CSV file, as its own source and dataset:
    python -m procurelake.ingestion.cli csv --source-system <src> --dataset <ds> --url <https url> \
        --key-columns id --local-root ./local_lake

The exit code is non-zero if any batch fails reconciliation.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.contracts.source import load_contracts
from procurelake.ingestion.connectors import canada_contracts
from procurelake.ingestion.connectors.csv_snapshot import CsvSnapshotConnector, CsvSource
from procurelake.ingestion.connectors.postgres_snapshot import PostgresSnapshotConnector
from procurelake.ingestion.connectors.usaspending import (
    UsaspendingConnector,
    plan_incremental,
    resweep_days,
    usaspending_http_client,
)
from procurelake.ingestion.landing import LandingStore, LocalLandingStore, S3LandingStore

DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "source"


def _stores(args: argparse.Namespace) -> tuple[LandingStore, LandingStore]:
    if args.local_root:
        root = Path(args.local_root)
        return LocalLandingStore(root / "raw"), LocalLandingStore(root / "artifacts")
    if not (args.raw_bucket and args.manifest_bucket):
        raise SystemExit("--raw-bucket and --manifest-bucket are required unless --local-root is given")
    session = build_session(region=args.region, role_arn=args.role_arn, session_name="procurelake-ingestion")
    s3 = session.client("s3")
    return S3LandingStore(s3, args.raw_bucket), S3LandingStore(s3, args.manifest_bucket)


def _run_postgres_snapshot(
    args: argparse.Namespace, raw_store: LandingStore, manifest_store: LandingStore
) -> int:
    dsn = os.environ.get("PROCURELAKE_PG_DSN")
    if not dsn:
        raise SystemExit("PROCURELAKE_PG_DSN is not set (see .env.example)")
    connector = PostgresSnapshotConnector(
        dsn,
        raw_store,
        manifest_store,
        load_contracts(Path(args.contracts_dir)),
        rows_per_file=args.rows_per_file,
        source_system=args.source_system,
    )
    manifests = connector.snapshot()
    for m in manifests:
        rec = m.reconciliation
        print(
            f"{m.dataset:<20} batch={m.batch_id} status={m.status} files={len(m.files)} "
            f"rows={rec.landed_record_count} expected={rec.expected_count_before} "
            f"detail={m.error or rec.detail}"
        )
    return 0 if all(m.status == "succeeded" for m in manifests) else 1


def _run_csv(args: argparse.Namespace, raw_store: LandingStore, manifest_store: LandingStore) -> int:
    if args.source == "canada-contracts":
        source = canada_contracts.SOURCE
        if args.path:
            source = dataclasses.replace(source, url=None)
    else:
        if not args.url and not args.path:
            raise SystemExit("csv needs --url or --path")
        name = Path(args.path).name if args.path else args.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        keys = tuple(k.strip() for k in (args.key_columns or "").split(",") if k.strip())
        source = CsvSource(
            source_system=args.source_system, dataset=args.dataset,
            schema_version=args.schema_version or f"{args.source_system}.{args.dataset}.v1",
            file_name=args.file_name or name or "data.csv", url=None if args.path else args.url,
            key_columns=keys, provenance_url=args.provenance_url, license=args.license,
        )  # fmt: skip
    connector = CsvSnapshotConnector(raw_store, manifest_store)
    if args.skip_if_unchanged and not args.path:
        unchanged = connector.unchanged_batch(source)
        if unchanged:
            print(
                f"{source.source_system}/{source.dataset}: unchanged since batch {unchanged} "
                "(same ETag and size); skipped"
            )
            return 0
    manifest = connector.ingest(
        source, local_path=Path(args.path) if args.path else None, expect_sha256=args.expect_sha256
    )
    rec = manifest.reconciliation
    print(
        f"{manifest.source_system}/{manifest.dataset} batch={manifest.batch_id} status={manifest.status} "
        f"records={rec.landed_record_count} unique_keys={rec.unique_record_count} "
        f"sha256={manifest.request.get('sha256')} detail={manifest.error or rec.detail}"
    )
    return 0 if manifest.status == "succeeded" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="source", required=True)
    usa = sub.add_parser("usaspending", help="USAspending.gov contract awards by last_modified_date")
    when = usa.add_mutually_exclusive_group(required=True)
    when.add_argument("--date", type=date.fromisoformat, help="single UTC day (YYYY-MM-DD)")
    when.add_argument("--since", type=date.fromisoformat, help="fill every missing day since this date")
    when.add_argument(
        "--resweep-days",
        type=int,
        help="collect the N most recent settled days again, as re-sweep batches (ADR 0044)",
    )
    usa.add_argument("--max-days", type=int, default=None, help="cap on days per incremental run")
    usa.add_argument("--raw-bucket")
    usa.add_argument("--manifest-bucket")
    usa.add_argument("--role-arn", help="IAM role to assume (e.g. procurelake-dev-ingestion)")
    usa.add_argument("--region", default=DEFAULT_REGION)
    usa.add_argument("--local-root", help="write to this directory instead of S3")
    usa.add_argument(
        "--allow-unsettled",
        action="store_true",
        help="collect a day inside the settling period (recorded as unsettled; does not complete the window)",
    )

    pg = sub.add_parser("postgres-snapshot", help="consistent full snapshot of the operational tables")
    pg.add_argument("--contracts-dir", default=str(DEFAULT_CONTRACTS_DIR))
    pg.add_argument("--rows-per-file", type=int, default=50_000)
    pg.add_argument(
        "--source-system",
        default="postgres",
        help="Bronze/manifest namespace (postgres_bench for the Week 10 benchmarks)",
    )
    canada = sub.add_parser(
        "canada-contracts", help="Government of Canada contracts over $10,000 (open data CSV)"
    )
    generic = sub.add_parser("csv", help="any CSV file as one snapshot batch")
    generic.add_argument("--source-system", required=True)
    generic.add_argument("--dataset", required=True)
    generic.add_argument("--url", help="HTTPS URL to download")
    generic.add_argument("--key-columns", help="comma-separated columns that must be unique and non-empty")
    generic.add_argument("--schema-version")
    generic.add_argument("--file-name", help="Bronze file name (default: from the URL or path)")
    generic.add_argument("--provenance-url")
    generic.add_argument("--license")
    for p in (canada, generic):
        p.add_argument("--path", help="land this local file (a pinned copy) instead of downloading")
        p.add_argument("--expect-sha256", help="fail unless the file has this SHA-256 (pinned snapshot)")
        p.add_argument(
            "--skip-if-unchanged",
            action="store_true",
            help="HEAD the URL first; skip when ETag and size equal the latest succeeded batch",
        )
    for p in (pg, canada, generic):
        p.add_argument("--raw-bucket")
        p.add_argument("--manifest-bucket")
        p.add_argument("--role-arn", help="IAM role to assume (e.g. procurelake-dev-ingestion)")
        p.add_argument("--region", default=DEFAULT_REGION)
        p.add_argument("--local-root", help="write to this directory instead of S3")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raw_store, manifest_store = _stores(args)
    if args.source == "postgres-snapshot":
        return _run_postgres_snapshot(args, raw_store, manifest_store)
    if args.source in {"canada-contracts", "csv"}:
        return _run_csv(args, raw_store, manifest_store)
    connector = UsaspendingConnector(usaspending_http_client(), raw_store, manifest_store)

    last_settled_day = datetime.now(UTC).date() - timedelta(days=connector.config.settle_days)
    purpose = "resweep" if args.resweep_days else "daily"
    if args.resweep_days:
        days = resweep_days(datetime.now(UTC).date(), connector.config.settle_days, args.resweep_days)
    elif args.date:
        days = [args.date]
    else:
        days = plan_incremental(manifest_store, args.since, last_settled_day, args.max_days)
    if not days:
        print("Nothing to ingest: every day in range already has a succeeded batch.")
        return 0

    failures = 0
    for day in days:
        manifest = connector.ingest_day(day, allow_unsettled=args.allow_unsettled, purpose=purpose)
        rec = manifest.reconciliation
        print(
            f"{day} batch={manifest.batch_id} status={manifest.status} settled={manifest.window_settled} "
            f"files={len(manifest.files)} "
            f"records={rec.landed_record_count if rec else 0} "
            f"expected={rec.expected_count_before if rec else None} "
            f"detail={rec.detail if rec else manifest.error}"
        )
        failures += manifest.status != "succeeded"
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
