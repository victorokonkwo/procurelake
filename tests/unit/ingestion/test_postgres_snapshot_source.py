"""The snapshot's source namespace: only ``postgres`` or ``postgres_<name>`` (no database needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from procurelake.contracts.source import load_contracts
from procurelake.ingestion.connectors.postgres_snapshot import PostgresSnapshotConnector
from procurelake.ingestion.landing import LocalLandingStore

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name", ["postgres", "postgres_bench"])
def test_accepts_postgres_namespaces(name, tmp_path):
    store = LocalLandingStore(tmp_path)
    c = PostgresSnapshotConnector(
        "postgresql://unused", store, store, load_contracts(REPO / "contracts" / "source"), source_system=name
    )
    assert c.source_system == name


@pytest.mark.parametrize("name", ["usaspending", "postgres_cdc/../x", "Postgres", "postgres-bench", ""])
def test_rejects_other_namespaces(name, tmp_path):
    store = LocalLandingStore(tmp_path)
    with pytest.raises(ValueError, match="source_system"):
        PostgresSnapshotConnector("postgresql://unused", store, store, [], source_system=name)
