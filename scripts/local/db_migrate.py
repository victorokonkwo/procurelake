"""Apply (or report on) operational PostgreSQL migrations.

Usage:
    uv run python scripts/local/db_migrate.py            # apply pending migrations
    uv run python scripts/local/db_migrate.py --status   # list applied / pending

Connection string is read from the PROCURELAKE_PG_DSN environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from procurelake.common.postgres_migrations import (
    MigrationError,
    apply_migrations,
    migration_status,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "database" / "postgres" / "migrations"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--status", action="store_true", help="show applied and pending migrations only")
    parser.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
    args = parser.parse_args()

    dsn = os.environ.get("PROCURELAKE_PG_DSN")
    if not dsn:
        print("PROCURELAKE_PG_DSN is not set (see .env.example).", file=sys.stderr)
        return 2

    try:
        if args.status:
            applied, pending = migration_status(dsn, args.migrations_dir)
            print("Applied:", *(applied or ["(none)"]), sep="\n  ")
            print("Pending:", *(pending or ["(none)"]), sep="\n  ")
        else:
            applied_now = apply_migrations(dsn, args.migrations_dir)
            if applied_now:
                print("Applied:", *applied_now, sep="\n  ")
            else:
                print("Database is up to date.")
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
