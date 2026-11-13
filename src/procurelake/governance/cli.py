"""Catalog governance commands (ADR 0042).

    python -m procurelake.governance.cli lineage --manifest dbt/target/manifest.json --out <file.md>
    python -m procurelake.governance.cli check --manifest dbt/target/manifest.json \\
        --silver-database <db> --gold-database <db> [--role-arn <arn>]

``lineage`` is offline. ``check`` is read-only (Glue ``GetTable`` only): it reports every Silver
table whose contract metadata, and every Gold model whose dbt meta, is not in Glue as declared,
and exits 1 on any drift or missing table. The daily maintenance job writes Silver; ``dbt build``
writes Gold (``persist_docs``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.governance.catalog import SyncResult, check_gold, check_silver, gold_expected
from procurelake.governance.lineage import gold_lineage, to_markdown
from procurelake.lakehouse.silver.resources import load_silver_specs


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
    results: list[SyncResult] = check_silver(glue, args.silver_database, load_silver_specs().values())
    results += check_gold(glue, args.gold_database, expected_gold)
    bad = [r for r in results if r.status != "unchanged"]
    for r in results:
        detail = f" ({', '.join(r.changed)})" if r.changed else ""
        print(f"{'OK   ' if r.status == 'unchanged' else 'FAIL '} {r.table}: {r.status}{detail}")
    print(f"{len(results)} tables checked, {len(bad)} not as declared")
    return 1 if bad else 0


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
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
