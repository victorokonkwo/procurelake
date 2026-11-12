"""Shared fixtures: the local PostgreSQL service (used by database/ and tests/) and a local
Spark session with Apache Iceberg (used by tests/integration/iceberg and tests/e2e).

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

# Local Spark with Iceberg, matching AWS Glue 5.1 (Spark 3.5, Iceberg 1.10.0). Spark is 3.5.8
# locally and 3.5.6 on Glue 5.1: the same 3.5 line (ADR 0024 amendment). A Hadoop catalog in a
# temporary warehouse directory, so no AWS access is needed. Requires Java 17: JAVA_HOME, or the
# Homebrew openjdk@17 location on macOS.

# Live-AWS fixtures and the opt-in skip for `aws` tests (make test-aws).
pytest_plugins = ["tests.aws_fixtures"]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "database" / "postgres" / "migrations"


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


ICEBERG_RUNTIME = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.10.0"  # Glue 5.1 bundles 1.10.0
HOMEBREW_JDK17 = Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    if not os.environ.get("JAVA_HOME"):
        if not HOMEBREW_JDK17.exists():
            pytest.fail("Java 17 is required for Spark tests: set JAVA_HOME")
        os.environ["JAVA_HOME"] = str(HOMEBREW_JDK17)
    from pyspark.sql import SparkSession

    warehouse = tmp_path_factory.mktemp("iceberg-warehouse")
    session = (
        SparkSession.builder.master("local[2]")
        .appName("procurelake-tests")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.jars.packages", ICEBERG_RUNTIME)
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.lake", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lake.type", "hadoop")
        .config("spark.sql.catalog.lake.warehouse", str(warehouse))
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()
