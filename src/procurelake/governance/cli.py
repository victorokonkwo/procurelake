"""Catalog governance commands (ADR 0042).

    python -m procurelake.governance.cli lineage --manifest dbt/target/manifest.json --out <file.md>
    python -m procurelake.governance.cli check --manifest dbt/target/manifest.json \\
        --silver-database <db> --gold-database <db> [--role-arn <arn>]

    python -m procurelake.governance.cli dbt-cloud-check --foundation-outputs <json> \\
        --dbt-cloud-outputs <json>

``lineage`` is offline. ``check`` is read-only (Glue ``GetTable`` only): it reports every Silver
table whose contract metadata, and every Gold model whose dbt meta, is not in Glue as declared,
and exits 1 on any drift or missing table. The daily maintenance job writes Silver; ``dbt build``
writes Gold (``persist_docs``). ``dbt-cloud-check`` runs the dbt Cloud user's policy through the
IAM policy simulator (ADR 0051); it reads ``terraform output -json`` of dev/foundation and
dev/dbt_cloud and exits 1 if any decision differs from ``dbt_cloud_access.expected_cases``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.governance import dbt_cloud_access
from procurelake.governance.catalog import SyncResult, check_gold, check_silver, gold_expected
from procurelake.governance.lineage import gold_lineage, to_markdown
from procurelake.lakehouse.silver.resources import load_reference, load_silver_specs


def _manifest(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"{p} not found: run `make dbt-parse` first")
    return json.loads(p.read_text())


def lineage(args: argparse.Namespace) -> int:
    report = to_markdown(gold_lineage(_manifest(args.manifest), load_silver_specs()))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    print(f"lineage written to {out}")
    return 0


def check(args: argparse.Namespace) -> int:
    expected_gold = gold_expected(_manifest(args.manifest))
    session = build_session(region=args.region, role_arn=args.role_arn, session_name="catalog-check")
    glue = session.client("glue")
    silver = [*load_silver_specs().values(), *load_reference().values()]
    results: list[SyncResult] = check_silver(glue, args.silver_database, silver)
    results += check_gold(glue, args.gold_database, expected_gold)
    bad = [r for r in results if r.status != "unchanged"]
    for r in results:
        detail = f" ({', '.join(r.changed)})" if r.changed else ""
        print(f"{'OK   ' if r.status == 'unchanged' else 'FAIL '} {r.table}: {r.status}{detail}")
    print(f"{len(results)} tables checked, {len(bad)} not as declared")
    return 1 if bad else 0


def _outputs(path: str) -> dict:
    return {k: v["value"] for k, v in json.loads(Path(path).read_text()).items()}


def dbt_cloud_check(args: argparse.Namespace) -> int:
    foundation = _outputs(args.foundation_outputs)
    dbt_cloud = _outputs(args.dbt_cloud_outputs)
    user_arn = dbt_cloud["user_arn"]
    names = dbt_cloud_access.Names(
        region=args.region,
        account_id=user_arn.split(":")[4],
        lake_bucket=foundation["bucket_names"]["lake"],
        raw_bucket=foundation["bucket_names"]["raw"],
        artifacts_bucket=foundation["bucket_names"]["artifacts"],
        silver_database=foundation["silver_database_name"],
        gold_database=foundation["gold_database_name"],
        sandbox_database=dbt_cloud["dbt_cloud_connection"]["schema"],
        dbt_cloud_workgroup=dbt_cloud["dbt_cloud_connection"]["work_group"],
        engineering_workgroup=foundation["athena_workgroup_name"],
        data_key_arn=foundation["data_kms_key_arn"],
        dbt_cloud_ip=args.dbt_cloud_ip,
    )
    iam = build_session(region=args.region).client("iam")
    result = dbt_cloud_access.report(
        dbt_cloud_access.simulate(iam, user_arn, dbt_cloud_access.expected_cases(names))
    )
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    lp = sub.add_parser("lineage", help="write the source-to-Gold lineage report (offline)")
    lp.add_argument("--manifest", default="dbt/target/manifest.json")
    lp.add_argument("--out", required=True)
    lp.set_defaults(func=lineage)
    cp = sub.add_parser("check", help="compare the Glue catalog with the contracts and dbt meta")
    cp.add_argument("--manifest", default="dbt/target/manifest.json")
    cp.add_argument("--silver-database", required=True)
    cp.add_argument("--gold-database", required=True)
    cp.add_argument("--role-arn")
    cp.add_argument("--region", default=DEFAULT_REGION)
    cp.set_defaults(func=check)
    dp = sub.add_parser("dbt-cloud-check", help="simulate the dbt Cloud user's policy (read-only)")
    dp.add_argument("--foundation-outputs", required=True, help="terraform output -json of dev/foundation")
    dp.add_argument("--dbt-cloud-outputs", required=True, help="terraform output -json of dev/dbt_cloud")
    # One of dbt Cloud's published us-east-1 addresses (variables.tf of dev/dbt_cloud).
    dp.add_argument("--dbt-cloud-ip", default="52.45.144.63")
    dp.add_argument("--region", default=DEFAULT_REGION)
    dp.set_defaults(func=dbt_cloud_check)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
