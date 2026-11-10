"""Forward-only SQL migration runner for the ProcureLake operational PostgreSQL database.

Migrations live in ``database/postgres/migrations`` and are named ``V<NNNN>__<description>.sql``.
Each pending migration runs in its own transaction together with its bookkeeping row in
``public.schema_migrations``, so a failed migration leaves no partial state behind.

Applied migrations are checksummed; editing an already-applied file is an error; write a new
migration instead. A session-level advisory lock prevents concurrent runners.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATION_FILENAME_PATTERN = re.compile(r"^V(?P<version>\d{4})__(?P<description>[a-z0-9_]+)\.sql$")

# Arbitrary, fixed key for pg_advisory_lock so only one runner applies migrations at a time.
ADVISORY_LOCK_KEY = 7_420_011_001

_CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version          text        PRIMARY KEY,
    filename         text        NOT NULL,
    checksum_sha256  text        NOT NULL,
    applied_at       timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """Raised when migrations on disk and in the database are inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    sql: str
    checksum_sha256: str


def discover_migrations(directory: Path) -> list[Migration]:
    """Load and validate all migration files in ``directory``, ordered by version."""
    if not directory.is_dir():
        raise MigrationError(f"Migration directory does not exist: {directory}")

    migrations: dict[str, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILENAME_PATTERN.match(path.name)
        if match is None:
            raise MigrationError(
                f"Invalid migration filename {path.name!r}; expected V<NNNN>__<description>.sql"
            )
        version = match.group("version")
        if version in migrations:
            raise MigrationError(
                f"Duplicate migration version {version}: {migrations[version].filename} and {path.name}"
            )
        raw = path.read_bytes()
        migrations[version] = Migration(
            version=version,
            filename=path.name,
            sql=raw.decode("utf-8"),
            checksum_sha256=hashlib.sha256(raw).hexdigest(),
        )
    return [migrations[v] for v in sorted(migrations)]


def _applied_checksums(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT version, filename, checksum_sha256 FROM public.schema_migrations ORDER BY version"
    ).fetchall()
    return {version: (filename, checksum) for version, filename, checksum in rows}


def pending_migrations(conn: psycopg.Connection, migrations: list[Migration]) -> list[Migration]:
    """Return migrations not yet applied, after verifying applied ones are unchanged."""
    applied = _applied_checksums(conn)
    on_disk = {m.version: m for m in migrations}

    missing = sorted(set(applied) - set(on_disk))
    if missing:
        raise MigrationError(f"Applied migrations missing from disk: {', '.join(missing)}")

    for version, (filename, checksum) in applied.items():
        if on_disk[version].checksum_sha256 != checksum:
            raise MigrationError(
                f"Checksum mismatch for applied migration {filename}; "
                "applied migrations must not be edited. Add a new migration instead."
            )

    pending = [m for m in migrations if m.version not in applied]
    if applied and pending and pending[0].version < max(applied):
        raise MigrationError(
            f"Migration {pending[0].filename} is older than the latest applied version "
            f"{max(applied)}; out-of-order migrations are not allowed."
        )
    return pending


def apply_migrations(dsn: str, directory: Path) -> list[str]:
    """Apply all pending migrations. Returns the filenames applied, in order."""
    migrations = discover_migrations(directory)
    applied_now: list[str] = []

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(_CREATE_HISTORY_TABLE)
            for migration in pending_migrations(conn, migrations):
                with conn.transaction():
                    # No parameters are passed, so psycopg sends the file as a simple query,
                    # which permits multiple statements and $$-quoted function bodies.
                    conn.execute(migration.sql.encode("utf-8"))
                    conn.execute(
                        "INSERT INTO public.schema_migrations (version, filename, checksum_sha256) "
                        "VALUES (%s, %s, %s)",
                        (migration.version, migration.filename, migration.checksum_sha256),
                    )
                applied_now.append(migration.filename)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))

    return applied_now


def migration_status(dsn: str, directory: Path) -> tuple[list[str], list[str]]:
    """Return (applied filenames, pending filenames) without changing the database."""
    migrations = discover_migrations(directory)
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()
        if not (exists and exists[0]):
            return [], [m.filename for m in migrations]
        applied = _applied_checksums(conn)
        pending = pending_migrations(conn, migrations)
    return [applied[v][0] for v in sorted(applied)], [m.filename for m in pending]
