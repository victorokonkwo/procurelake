"""Fixtures for tests against the local PostgreSQL service.

Each test session creates a throwaway database, applies all migrations to it with the real
migration runner, and drops it afterwards. Each test runs inside a transaction that is
rolled back, so tests never see each other's rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from procurelake.common.postgres_migrations import apply_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _base_dsn() -> str:
    dsn = os.environ.get("PROCURELAKE_PG_DSN")
    if not dsn:
        pytest.fail("PROCURELAKE_PG_DSN is not set. Run `make db-up` and export it from .env.")
    return dsn


@pytest.fixture(scope="session")
def scratch_db_factory() -> Iterator:
    """Create empty, uniquely named databases; drop them all at session end."""
    base_dsn = _base_dsn()
    created: list[str] = []

    def create() -> str:
        name = f"procurelake_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        params = conninfo_to_dict(base_dsn)
        params["dbname"] = name
        return make_conninfo(**params)

    yield create

    with psycopg.connect(base_dsn, autocommit=True) as admin:
        for name in created:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def migrated_dsn(scratch_db_factory) -> str:
    dsn = scratch_db_factory()
    apply_migrations(dsn, MIGRATIONS_DIR)
    return dsn


@pytest.fixture
def conn(migrated_dsn: str) -> Iterator[psycopg.Connection]:
    """Connection whose work is always rolled back after the test."""
    with psycopg.connect(migrated_dsn) as connection:
        yield connection
        connection.rollback()
