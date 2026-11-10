"""Prepare the dev RDS source for CDC (phase 1 of ADR 0027), as the operator.

1. Read the RDS-managed master secret and the DMS user's secret (Secrets Manager).
2. Connect over TLS and check that logical replication is on (wal_level = logical).
3. Apply the operational-schema migrations.
4. Load the seeded synthetic dataset if the database is empty (same seed as local: seed 42).
5. Create/update the DMS user: rds_replication + SELECT on the procurement schema.

Usage: uv run python scripts/aws/cdc_source_setup.py --host H --port P --database D \\
           --master-secret-arn ARN --replication-secret-arn ARN [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from procurelake.cdc.source_setup import replication_user_privileges, setup_replication_user
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.postgres_migrations import apply_migrations
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset, table_counts

REPO = Path(__file__).resolve().parents[2]


def _secret(sm, arn: str) -> dict:
    return json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--master-secret-arn", required=True)
    parser.add_argument("--replication-secret-arn", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)

    sm = build_session(region=args.region).client("secretsmanager")
    master, replication = _secret(sm, args.master_secret_arn), _secret(sm, args.replication_secret_arn)
    # TLS is required by the parameter group (rds.force_ssl=1). The server certificate is not
    # verified against the RDS CA bundle yet (ADR 0027, known gap).
    dsn = psycopg.conninfo.make_conninfo(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=master["username"],
        password=master["password"],
        sslmode="require",
        connect_timeout=15,
    )

    with psycopg.connect(dsn, autocommit=True) as conn:
        wal_level = conn.execute("SHOW wal_level").fetchone()[0]
        version = conn.execute("SHOW server_version").fetchone()[0]
        print(f"server_version={version} wal_level={wal_level}")
        if wal_level != "logical":
            print("wal_level is not logical: check rds.logical_replication / reboot", file=sys.stderr)
            return 1

    applied = apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
    print(f"migrations applied this run: {applied}")

    with psycopg.connect(dsn) as conn:
        counts = table_counts(conn)
        if any(counts.values()):
            print(f"source already has data, not seeding: {counts}")
        else:
            dataset = generate_dataset(
                load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=args.seed
            )
            loaded = load_dataset(conn, dataset)
            print(f"loaded seed={args.seed} fingerprint={dataset.fingerprint()}: {loaded}")

    with psycopg.connect(dsn, autocommit=True) as conn:
        setup_replication_user(conn, replication["username"], replication["password"])
        privs = replication_user_privileges(conn, replication["username"])
    print(
        json.dumps(
            {
                "replication_user": replication["username"],
                "member_of": privs["member_of"],
                "superuser": privs["superuser"],
                "tables_select": sum(t["select"] for t in privs["tables"].values()),
                "tables_write": sum(t["write"] for t in privs["tables"].values()),
                "schema_create": privs["schema_create"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
