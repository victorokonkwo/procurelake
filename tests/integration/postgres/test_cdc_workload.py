"""The CDC workload applies valid, deterministic changes (local PostgreSQL)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from procurelake.common.postgres_migrations import apply_migrations
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset, table_counts
from procurelake.data_generator.workload import run_workload

pytestmark = pytest.mark.postgres
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=42)


@pytest.fixture
def seeded(scratch_db_factory, dataset):
    def make():
        dsn = scratch_db_factory()
        apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
        with psycopg.connect(dsn) as c:
            load_dataset(c, dataset)
        return dsn

    return make


def _run(dsn, seed=7, n=150):
    with psycopg.connect(dsn) as c:
        return run_workload(c, seed=seed, operations=n)


def test_workload_covers_inserts_updates_and_deletes(seeded, dataset):
    dsn = seeded()
    result = _run(dsn)
    kinds = result.by_kind()
    assert kinds.get("I", 0) > 0 and kinds.get("U", 0) > 0 and kinds.get("D", 0) > 0
    ops = {c.operation for c in result.changes}
    assert {"pay_invoice", "new_invoice", "delete_invoice", "settle_purchase_order"} <= ops

    with psycopg.connect(dsn) as c:
        counts = table_counts(c)
        inserted = sum(1 for x in result.changes if x.kind == "I")
        deleted = sum(1 for x in result.changes if x.kind == "D")
        assert counts["invoices"] == len(dataset.invoices) + inserted - deleted
        for change in result.changes:
            if change.kind == "D":
                assert (
                    c.execute(
                        "SELECT 1 FROM procurement.invoices WHERE invoice_id = %s", (change.key,)
                    ).fetchone()
                    is None
                )


def test_after_images_match_the_database_and_rules(seeded):
    dsn = seeded()
    result = _run(dsn)
    latest = {}
    for change in result.changes:
        latest[(change.table, change.key)] = change
    with psycopg.connect(dsn) as c:
        for (table, key), change in latest.items():
            if change.kind == "D" or table != "invoices":
                continue
            row = c.execute(
                "SELECT payment_status, paid_amount, invoice_amount FROM procurement.invoices "
                "WHERE invoice_id = %s",
                (key,),
            ).fetchone()
            assert row == (
                change.after["payment_status"],
                change.after["paid_amount"],
                change.after["invoice_amount"],
            )


def test_settlement_changes_two_tables_in_one_transaction(seeded):
    result = _run(seeded())
    settle = [c for c in result.changes if c.operation == "settle_purchase_order"]
    by_tx = {}
    for c in settle:
        by_tx.setdefault(c.transaction_id, set()).add(c.table)
    assert by_tx and all(tables == {"invoices", "purchase_orders"} for tables in by_tx.values())


def test_same_state_and_seed_give_the_same_changes(seeded):
    first, second = _run(seeded()), _run(seeded())
    strip = lambda r: [(c.operation, c.table, c.kind, c.key) for c in r.changes]  # noqa: E731
    assert strip(first) == strip(second)
    assert strip(first) != strip(_run(seeded(), seed=8))
