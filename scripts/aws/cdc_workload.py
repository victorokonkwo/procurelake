"""Apply the CDC workload (procurelake.data_generator.workload) to the dev RDS source.

Connects as the RDS master user (RDS-managed secret) over TLS, applies N operations, and writes
every change (table, operation, key, commit-side timestamp) to build/evidence/ so CDC landing in
Bronze, and later Silver/Gold, can be checked against it.

Usage: uv run python scripts/aws/cdc_workload.py --host H --port P --database D \\
           --master-secret-arn ARN [--seed 7] [--operations 40]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.data_generator.workload import run_workload

EVIDENCE = Path(__file__).resolve().parents[2] / "build" / "evidence"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--master-secret-arn", required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--operations", type=int, default=40)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)

    sm = build_session(region=args.region).client("secretsmanager")
    master = json.loads(sm.get_secret_value(SecretId=args.master_secret_arn)["SecretString"])
    dsn = psycopg.conninfo.make_conninfo(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=master["username"],
        password=master["password"],
        sslmode="require",
        connect_timeout=15,
    )
    started = datetime.now(UTC)
    with psycopg.connect(dsn) as conn:
        result = run_workload(conn, seed=args.seed, operations=args.operations)
    record = {
        "started_at": started.isoformat(),
        "seed": args.seed,
        "operations": args.operations,
        "by_kind": result.by_kind(),
        "skipped": result.skipped,
        "changes": [
            {
                "operation": c.operation,
                "table": c.table,
                "kind": c.kind,
                "key": c.key,
                "tenant_id": c.tenant_id,
                "transaction_id": c.transaction_id,
                "committed_at": c.committed_at.isoformat(),
            }
            for c in result.changes
        ],
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"cdc_workload_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    by_table: dict[str, dict[str, int]] = {}
    for c in result.changes:
        by_table.setdefault(c.table, {}).setdefault(c.kind, 0)
        by_table[c.table][c.kind] += 1
    print(json.dumps({"evidence": str(out.relative_to(EVIDENCE.parents[1])), "by_table": by_table}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
