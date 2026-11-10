"""Snapshot connector tests against real PostgreSQL (scratch database seeded with the
deterministic synthetic dataset) and a local landing store."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from procurelake.common.postgres_migrations import apply_migrations
from procurelake.contracts.source import load_contracts
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset
from procurelake.ingestion.connectors.postgres_snapshot import TABLE_ORDER, PostgresSnapshotConnector
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.ingestion.manifest import load_manifests

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = load_contracts(REPO / "contracts" / "source")


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=42)


@pytest.fixture
def seeded_dsn(scratch_db_factory, dataset) -> str:
    dsn = scratch_db_factory()
    apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
    with psycopg.connect(dsn) as conn:
        load_dataset(conn, dataset)
    return dsn


@pytest.fixture
def stores(tmp_path):
    return LocalLandingStore(tmp_path / "raw"), LocalLandingStore(tmp_path / "artifacts")


def _rows(raw_store, manifest) -> list[dict]:
    rows = []
    for f in manifest.files:
        for line in raw_store.get(f.key).decode().splitlines():
            rows.append(json.loads(line, parse_float=Decimal))
    return rows


def test_snapshot_lands_every_table_and_reconciles(seeded_dsn, stores, dataset):
    raw, manifests_store = stores
    manifests = PostgresSnapshotConnector(seeded_dsn, raw, manifests_store, CONTRACTS).snapshot()

    assert [m.dataset for m in manifests] == list(TABLE_ORDER)
    assert all(m.status == "succeeded" for m in manifests), [(m.dataset, m.error) for m in manifests]
    assert len({m.batch_id for m in manifests}) == 1  # one batch across all tables
    expected = {**dataset.counts(), "contract_documents": 0}
    assert {m.dataset: m.reconciliation.landed_record_count for m in manifests} == expected
    for m in manifests:
        assert m.request["pg_snapshot"] and m.request["wal_lsn"]
        assert m.schema_version == f"postgres.procurement.{m.dataset}.v1"
    persisted = load_manifests(manifests_store, "postgres", "awards")
    assert [p.batch_id for p in persisted] == [manifests[0].batch_id]


def test_values_are_landed_exactly_as_postgres_renders_them(seeded_dsn, stores, dataset):
    raw, manifests_store = stores
    manifests = {
        m.dataset: m
        for m in PostgresSnapshotConnector(seeded_dsn, raw, manifests_store, CONTRACTS).snapshot()
    }

    landed_awards = {r["award_id"]: r for r in _rows(raw, manifests["awards"])}
    for award in dataset.awards[:50]:
        row = landed_awards[str(award.award_id)]
        assert row["award_amount"] == award.award_amount  # exact decimal, not a float
        assert row["currency_code"] == award.currency_code
        assert row["tenant_id"] == award.tenant_id
    assert set(landed_awards) == {str(a.award_id) for a in dataset.awards}

    first_line = raw.get(manifests["invoices"].files[0].key).decode().splitlines()[0]
    assert '"paid_amount":' in first_line and "e+" not in first_line


def test_rows_are_split_across_files(seeded_dsn, stores, dataset):
    raw, manifests_store = stores
    manifests = {
        m.dataset: m
        for m in PostgresSnapshotConnector(
            seeded_dsn, raw, manifests_store, CONTRACTS, rows_per_file=500
        ).snapshot()
    }
    invoices = manifests["invoices"]
    assert len(invoices.files) == math.ceil(len(dataset.invoices) / 500)
    assert sum(f.record_count for f in invoices.files) == len(dataset.invoices)
    assert manifests["contract_documents"].files == []
    assert manifests["contract_documents"].status == "succeeded"


class _InsertDuringSnapshot(LocalLandingStore):
    """Commits a new supplier from a separate connection while the snapshot is in progress."""

    def __init__(self, root, dsn):
        super().__init__(root)
        self.dsn = dsn
        self.done = False

    def put_new(self, key, data, content_type):
        if not self.done:
            self.done = True
            with psycopg.connect(self.dsn, autocommit=True) as other:
                other.execute(
                    "INSERT INTO procurement.suppliers (tenant_id, supplier_name, category) "
                    "VALUES ('tenant_alder', 'Late Supplier Ltd.', 'IT services')"
                )
        return super().put_new(key, data, content_type)


def test_snapshot_is_consistent_despite_concurrent_writes(seeded_dsn, tmp_path, dataset):
    raw = _InsertDuringSnapshot(tmp_path / "raw", seeded_dsn)
    manifests = {
        m.dataset: m
        for m in PostgresSnapshotConnector(
            seeded_dsn, raw, LocalLandingStore(tmp_path / "art"), CONTRACTS
        ).snapshot()
    }
    assert raw.done  # the insert happened during the snapshot (while landing tenants)
    assert manifests["suppliers"].reconciliation.landed_record_count == len(dataset.suppliers)
    with psycopg.connect(seeded_dsn) as conn:
        assert (
            conn.execute("SELECT count(*) FROM procurement.suppliers").fetchone()[0]
            == len(dataset.suppliers) + 1
        )


def test_schema_drift_fails_only_the_drifted_table(seeded_dsn, stores):
    with psycopg.connect(seeded_dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE procurement.awards ADD COLUMN notes text")
    raw, manifests_store = stores
    manifests = {
        m.dataset: m
        for m in PostgresSnapshotConnector(seeded_dsn, raw, manifests_store, CONTRACTS).snapshot()
    }

    assert manifests["awards"].status == "failed"
    assert "unexpected column notes" in manifests["awards"].error
    assert manifests["awards"].files == []
    assert all(m.status == "succeeded" for name, m in manifests.items() if name != "awards")


def test_snapshot_transaction_is_read_only(seeded_dsn, stores, monkeypatch):
    seen = {}
    original = PostgresSnapshotConnector._snapshot_table

    def spy(self, conn, contract, *args):
        if contract.name == "tenants":
            seen["read_only"] = conn.execute("SHOW transaction_read_only").fetchone()[0]
            seen["isolation"] = conn.execute("SHOW transaction_isolation").fetchone()[0]
        return original(self, conn, contract, *args)

    monkeypatch.setattr(PostgresSnapshotConnector, "_snapshot_table", spy)
    raw, manifests_store = stores
    PostgresSnapshotConnector(seeded_dsn, raw, manifests_store, CONTRACTS).snapshot()
    assert seen == {"read_only": "on", "isolation": "repeatable read"}
