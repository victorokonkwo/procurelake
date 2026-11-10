"""Integration tests: the generated dataset loads into PostgreSQL and round-trips exactly."""

from __future__ import annotations

from dataclasses import astuple
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from procurelake.common.postgres_migrations import apply_migrations
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import TargetNotEmptyError, load_dataset, table_counts
from procurelake.data_generator.model import column_names

pytestmark = pytest.mark.postgres

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "configs" / "tenants" / "synthetic_tenants.yaml"
MIGRATIONS = REPO_ROOT / "database" / "postgres" / "migrations"


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(load_config(CONFIG), seed=42)


@pytest.fixture
def fresh_dsn(scratch_db_factory) -> str:
    dsn = scratch_db_factory()
    apply_migrations(dsn, MIGRATIONS)
    return dsn


def test_dataset_satisfies_every_database_constraint_and_round_trips(fresh_dsn, dataset):
    with psycopg.connect(fresh_dsn) as conn:
        counts = load_dataset(conn, dataset)

    assert {t: n for t, n in counts.items() if t != "contract_documents"} == dataset.counts()

    with psycopg.connect(fresh_dsn) as conn:
        for table, row_type, rows in dataset.tables():
            columns = column_names(row_type)
            pk = columns[0]
            query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                sql.Identifier("procurement", table),
                sql.Identifier(pk),
            )
            from_db = conn.execute(query).fetchall()
            expected = sorted((astuple(r) for r in rows), key=lambda t: str(t[0]))
            from_db = sorted(from_db, key=lambda t: str(t[0]))
            assert [tuple(r) for r in from_db] == expected, table


def test_refuses_to_load_into_non_empty_tables(fresh_dsn, dataset):
    with psycopg.connect(fresh_dsn) as conn:
        load_dataset(conn, dataset)
        with pytest.raises(TargetNotEmptyError):
            load_dataset(conn, dataset)
        assert table_counts(conn)["tenants"] == 2


def test_reset_replaces_contents_atomically(fresh_dsn, dataset):
    with psycopg.connect(fresh_dsn) as conn:
        load_dataset(conn, dataset)
        counts = load_dataset(conn, dataset, reset=True)
    assert counts["invoices"] == len(dataset.invoices)


def test_tenant_rows_are_isolated_in_database(fresh_dsn, dataset):
    """Database-side check: no row's parent belongs to another tenant."""
    with psycopg.connect(fresh_dsn) as conn:
        load_dataset(conn, dataset)
        cross_tenant = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM procurement.awards a
                 JOIN procurement.suppliers s ON s.supplier_id = a.supplier_id
                WHERE s.tenant_id <> a.tenant_id)
            + (SELECT count(*) FROM procurement.invoices i
                 JOIN procurement.purchase_orders p ON p.po_id = i.po_id
                WHERE p.tenant_id <> i.tenant_id)
            + (SELECT count(*) FROM procurement.purchase_orders p
                 JOIN procurement.agencies g ON g.agency_id = p.agency_id
                WHERE g.tenant_id <> p.tenant_id)
            """
        ).fetchone()[0]
    assert cross_tenant == 0
