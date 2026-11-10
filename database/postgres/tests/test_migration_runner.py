"""Integration tests for the migration runner against a real PostgreSQL database."""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from procurelake.common.postgres_migrations import (
    MigrationError,
    apply_migrations,
    migration_status,
)

pytestmark = pytest.mark.postgres

REAL_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_apply_is_idempotent(scratch_db_factory):
    dsn = scratch_db_factory()
    first = apply_migrations(dsn, REAL_MIGRATIONS)
    second = apply_migrations(dsn, REAL_MIGRATIONS)
    assert first == sorted(p.name for p in REAL_MIGRATIONS.glob("V*.sql"))
    assert second == []
    applied, pending = migration_status(dsn, REAL_MIGRATIONS)
    assert applied == first
    assert pending == []


def test_edited_applied_migration_is_detected(scratch_db_factory, tmp_path):
    dsn = scratch_db_factory()
    (tmp_path / "V0001__create_t.sql").write_text("CREATE TABLE t (id int);")
    apply_migrations(dsn, tmp_path)

    (tmp_path / "V0001__create_t.sql").write_text("CREATE TABLE t (id bigint);")
    with pytest.raises(MigrationError, match="Checksum mismatch"):
        apply_migrations(dsn, tmp_path)


def test_failed_migration_is_rolled_back_completely(scratch_db_factory, tmp_path):
    dsn = scratch_db_factory()
    (tmp_path / "V0001__partial.sql").write_text(
        "CREATE TABLE should_not_exist (id int);\nSELECT * FROM table_that_does_not_exist;"
    )
    with pytest.raises(psycopg.errors.UndefinedTable):
        apply_migrations(dsn, tmp_path)

    with psycopg.connect(dsn) as conn:
        exists = conn.execute("SELECT to_regclass('public.should_not_exist')").fetchone()[0]
        recorded = conn.execute("SELECT count(*) FROM public.schema_migrations").fetchone()[0]
    assert exists is None
    assert recorded == 0


def test_out_of_order_migration_rejected(scratch_db_factory, tmp_path):
    dsn = scratch_db_factory()
    shutil.copy(REAL_MIGRATIONS / "V0001__operational_schema.sql", tmp_path)
    (tmp_path / "V0003__later.sql").write_text("SELECT 1;")
    apply_migrations(dsn, tmp_path)

    (tmp_path / "V0002__earlier.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="out-of-order"):
        apply_migrations(dsn, tmp_path)
