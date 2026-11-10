"""Generate the synthetic procurement dataset and load it into the operational database.

Usage:
    uv run python scripts/local/seed_db.py                 # load into an empty database
    uv run python scripts/local/seed_db.py --reset         # truncate procurement tables first
    uv run python scripts/local/seed_db.py --dry-run       # generate only; print counts + fingerprint
    uv run python scripts/local/seed_db.py --seed 7 --scale 10

The same --seed, --scale, and config always produce the same dataset (same fingerprint).
The connection string comes from PROCURELAKE_PG_DSN. Only local hosts are allowed unless
--allow-remote-host is given.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from procurelake.data_generator.config import load_config, scaled
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import TargetNotEmptyError, load_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "tenants" / "synthetic_tenants.yaml"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _print_counts(title: str, counts: dict[str, int]) -> None:
    print(title)
    for table, count in counts.items():
        print(f"  {table:<20} {count:>10,}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scale", type=float, default=1.0, help="multiply supplier/solicitation counts")
    parser.add_argument("--reset", action="store_true", help="truncate all procurement tables first")
    parser.add_argument("--dry-run", action="store_true", help="generate only; do not connect")
    parser.add_argument("--allow-remote-host", action="store_true")
    args = parser.parse_args()

    config = scaled(load_config(args.config), args.scale)
    dataset = generate_dataset(config, seed=args.seed)
    _print_counts(f"Generated (seed={args.seed}, scale={args.scale}):", dataset.counts())
    print(f"Fingerprint: {dataset.fingerprint()}")
    if args.dry_run:
        return 0

    dsn = os.environ.get("PROCURELAKE_PG_DSN")
    if not dsn:
        print("PROCURELAKE_PG_DSN is not set (see .env.example).", file=sys.stderr)
        return 2
    host = conninfo_to_dict(dsn).get("host", "")
    if host not in LOCAL_HOSTS and not args.allow_remote_host:
        print(f"Refusing to load into non-local host {host!r} without --allow-remote-host.", file=sys.stderr)
        return 2

    try:
        with psycopg.connect(dsn) as conn:
            counts = load_dataset(conn, dataset, reset=args.reset)
    except TargetNotEmptyError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    _print_counts("Loaded:", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
