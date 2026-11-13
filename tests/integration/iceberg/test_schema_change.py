"""Acceptance test 6: an approved nullable field evolves the pipeline without data loss.

Plan §3: "Add an approved nullable field → the pipeline evolves without data loss." End to end,
locally: PostgreSQL gains a column, its source contract gains it as nullable (the only change
ADR 0022 allows within a contract version), and the next snapshot flows into the existing
Silver Iceberg table:

- Silver gains the column by Iceberg schema evolution (metadata only);
- every row and every existing value is unchanged;
- rows that have the new value carry it, the others read null;
- a NOT NULL addition is refused before anything is written.
"""

from __future__ import annotations

import itertools
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
import yaml

from procurelake.common.postgres_migrations import apply_migrations
from procurelake.contracts.source import load_contracts
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset
from procurelake.ingestion.connectors.postgres_snapshot import PostgresSnapshotConnector
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.lakehouse.silver.pipeline import run_table
from procurelake.lakehouse.silver.spec import load_specs
from procurelake.lakehouse.silver.writer import SchemaEvolutionError

pytestmark = [pytest.mark.spark, pytest.mark.postgres]

REPO = Path(__file__).resolve().parents[3]
CONFIGS = REPO / "configs" / "pipelines" / "silver"
NEW_COLUMN = {"name": "website", "type": "text", "nullable": True, "description": "Supplier website (added)."}


def _contracts_with(tmp_path: Path, column: dict) -> Path:
    """A copy of the source contracts where suppliers has one more column."""
    target = tmp_path / f"contracts_{column['name']}_{column['nullable']}"
    shutil.copytree(REPO / "contracts" / "source", target)
    path = target / "suppliers.yaml"
    contract = yaml.safe_load(path.read_text())
    contract["columns"].append(column)
    path.write_text(yaml.safe_dump(contract, sort_keys=False))
    return target


@pytest.fixture
def env(scratch_db_factory, tmp_path, spark):
    dsn = scratch_db_factory()
    apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
    dataset = generate_dataset(load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=42)
    with psycopg.connect(dsn) as conn:
        load_dataset(conn, dataset)
    raw, art = LocalLandingStore(tmp_path / "raw"), LocalLandingStore(tmp_path / "art")
    ns = f"lake.sc_{tmp_path.name.replace('-', '_')[-20:]}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    taken, start = itertools.count(), datetime.now(UTC)

    def snapshot(contracts_dir: Path) -> dict:
        when = start + timedelta(seconds=next(taken))  # batch ids order by second
        contracts = load_contracts(contracts_dir)
        manifests = PostgresSnapshotConnector(dsn, raw, art, contracts, clock=lambda: when).snapshot()
        return {m.dataset: m for m in manifests}

    return {
        "dsn": dsn,
        "snapshot": snapshot,
        "table": f"{ns}.silver_suppliers",
        "q": (tmp_path / "q").as_uri(),
    }


def _apply(spark, env, contracts_dir: Path, manifests: dict):
    spec = load_specs(load_contracts(contracts_dir), CONFIGS)["suppliers"]
    return run_table(spark, spec, manifests["suppliers"], env["table"], env["q"])


def _rows(spark, table: str, columns: list[str]) -> dict:
    return {
        r.supplier_id: tuple(r[c] for c in columns) for r in spark.table(table).select(*columns).collect()
    }


def test_nullable_field_evolves_silver_without_data_loss(spark, env, tmp_path):
    # Before: Silver built from the current contract.
    first = _apply(spark, env, REPO / "contracts" / "source", env["snapshot"](REPO / "contracts" / "source"))
    assert first.status == "applied"
    contract = next(c for c in load_contracts(REPO / "contracts" / "source") if c.name == "suppliers")
    columns = [col.name for col in contract.columns]
    before = _rows(spark, env["table"], columns)

    # The approved change: a nullable column in PostgreSQL (a migration) and in the contract.
    with psycopg.connect(env["dsn"]) as conn:
        conn.execute("ALTER TABLE procurement.suppliers ADD COLUMN website text")
        updated = conn.execute(
            "UPDATE procurement.suppliers SET website = 'https://example.org/' || supplier_id "
            "WHERE supplier_id IN "
            "(SELECT supplier_id FROM procurement.suppliers ORDER BY supplier_id LIMIT 5) "
            "RETURNING supplier_id::text"
        ).fetchall()
    evolved_contracts = _contracts_with(tmp_path, NEW_COLUMN)
    second = _apply(spark, env, evolved_contracts, env["snapshot"](evolved_contracts))

    assert second.status == "applied"
    assert "website" in spark.table(env["table"]).columns
    after = _rows(spark, env["table"], [*columns, "website"])
    assert len(after) == len(before), "rows were lost or duplicated"
    changed = {
        k for k, v in after.items() if v[: len(columns)] != before[k] and k not in {u[0] for u in updated}
    }
    # The 5 updated rows changed updated_at (trigger); every other row is byte-for-byte the same.
    assert not changed, f"existing values changed for {sorted(changed)[:3]}"
    with_site = {k for k, v in after.items() if v[-1] is not None}
    assert with_site == {u[0] for u in updated}
    assert all(after[k][-1] == f"https://example.org/{k}" for k in with_site)
    props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {env['table']}").collect()}
    assert props["procurelake.contract_version"] == "1"


def test_a_not_null_addition_is_refused_before_anything_is_written(spark, env, tmp_path):
    _apply(spark, env, REPO / "contracts" / "source", env["snapshot"](REPO / "contracts" / "source"))
    snapshots_before = spark.sql(f"SELECT count(*) FROM {env['table']}.snapshots").first()[0]
    with psycopg.connect(env["dsn"]) as conn:
        conn.execute("ALTER TABLE procurement.suppliers ADD COLUMN tier text NOT NULL DEFAULT 'standard'")
    required = _contracts_with(
        tmp_path, {"name": "tier", "type": "text", "nullable": False, "description": "x"}
    )

    with pytest.raises(SchemaEvolutionError, match="NOT NULL"):
        _apply(spark, env, required, env["snapshot"](required))
    assert "tier" not in spark.table(env["table"]).columns
    assert spark.sql(f"SELECT count(*) FROM {env['table']}.snapshots").first()[0] == snapshots_before
