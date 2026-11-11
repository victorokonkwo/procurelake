"""End-to-end: PostgreSQL -> snapshot into Bronze (local store) -> Silver Iceberg (local Spark).

Covers the Week 3 gate locally (query-ready Silver Iceberg tables) and the Silver apply rules:
reconciliation with source, exact values, idempotent re-apply, source updates/deletes, rejected
rows keeping their previous version, stale batches refused, and the reject-ratio quality gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from procurelake.common.postgres_migrations import apply_migrations
from procurelake.contracts.source import load_contracts
from procurelake.data_generator.config import load_config
from procurelake.data_generator.generate import generate_dataset
from procurelake.data_generator.load import load_dataset
from procurelake.ingestion.connectors.postgres_snapshot import PostgresSnapshotConnector
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.lakehouse.silver.job import run_batch
from procurelake.lakehouse.silver.pipeline import BronzeMismatchError, run_table, select_snapshot_batch
from procurelake.lakehouse.silver.spec import load_specs
from procurelake.lakehouse.silver.writer import StaleBatchError

pytestmark = [pytest.mark.spark, pytest.mark.postgres]

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = load_contracts(REPO / "contracts" / "source")
SPECS = load_specs(CONTRACTS, REPO / "configs" / "pipelines" / "silver")


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(load_config(REPO / "configs" / "tenants" / "synthetic_tenants.yaml"), seed=42)


@pytest.fixture
def env(scratch_db_factory, dataset, tmp_path, spark):
    dsn = scratch_db_factory()
    apply_migrations(dsn, REPO / "database" / "postgres" / "migrations")
    with psycopg.connect(dsn) as conn:
        load_dataset(conn, dataset)
    raw, art = LocalLandingStore(tmp_path / "raw"), LocalLandingStore(tmp_path / "art")
    namespace = f"lake.it_{tmp_path.name.replace('-', '_')[-20:]}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")

    def snapshot():
        return {m.dataset: m for m in PostgresSnapshotConnector(dsn, raw, art, CONTRACTS).snapshot()}

    return {
        "dsn": dsn,
        "snapshot": snapshot,
        "ns": namespace,
        "artifacts": art,
        "quarantine": (tmp_path / "quarantine").as_uri(),
    }


def _run(spark, env, manifests, table):
    return run_table(spark, SPECS[table], manifests[table], f"{env['ns']}.silver_{table}", env["quarantine"])


def test_first_snapshot_builds_every_silver_table(spark, env, dataset):
    manifests = env["snapshot"]()
    counts = dataset.counts()
    for table in SPECS:
        run = _run(spark, env, manifests, table)
        assert run.status == "applied" and run.rejected_rows == 0, run
        assert run.apply.rows_after == counts[table]

    silver_awards = {r.award_id: r for r in spark.table(f"{env['ns']}.silver_awards").collect()}
    for award in dataset.awards[:100]:
        row = silver_awards[str(award.award_id)]
        assert row.award_amount == award.award_amount
        assert row.currency_code == award.currency_code
        assert row._batch_id == manifests["awards"].batch_id
    by_tenant = spark.sql(
        f"SELECT tenant_id, count(*) n FROM {env['ns']}.silver_invoices GROUP BY tenant_id"
    ).collect()
    assert {r.tenant_id: r.n for r in by_tenant} == {
        t: sum(1 for i in dataset.invoices if i.tenant_id == t) for t in ("tenant_alder", "tenant_birch")
    }


def test_reapplying_the_same_batch_is_a_no_op(spark, env):
    manifests = env["snapshot"]()
    first = _run(spark, env, manifests, "suppliers")
    second = _run(spark, env, manifests, "suppliers")
    assert first.status == "applied" and second.status == "skipped"
    assert second.apply.rows_after == first.apply.rows_after


def test_source_updates_and_deletes_flow_to_silver(spark, env, dataset):
    first = env["snapshot"]()
    for table in (
        "tenants",
        "agencies",
        "suppliers",
        "solicitations",
        "awards",
        "purchase_orders",
        "invoices",
    ):
        _run(spark, env, first, table)

    victim_invoice = dataset.invoices[0].invoice_id
    changed_award = dataset.awards[5].award_id
    with psycopg.connect(env["dsn"], autocommit=True) as conn:
        conn.execute("DELETE FROM procurement.invoices WHERE invoice_id = %s", (victim_invoice,))
        conn.execute(
            "UPDATE procurement.awards SET status = 'cancelled' WHERE award_id = %s", (changed_award,)
        )

    second = env["snapshot"]()
    assert second["invoices"].batch_id > first["invoices"].batch_id
    _run(spark, env, second, "invoices")
    _run(spark, env, second, "awards")

    ns = env["ns"]
    assert (
        spark.sql(f"SELECT count(*) c FROM {ns}.silver_invoices WHERE invoice_id = '{victim_invoice}'")
        .first()
        .c
        == 0
    )
    award = spark.sql(
        f"SELECT status, _batch_id FROM {ns}.silver_awards WHERE award_id = '{changed_award}'"
    ).first()
    assert (award.status, award._batch_id) == ("cancelled", second["awards"].batch_id)
    unchanged = spark.sql(
        f"SELECT _batch_id FROM {ns}.silver_awards WHERE award_id = '{dataset.awards[6].award_id}'"
    ).first()
    assert unchanged._batch_id == first["awards"].batch_id  # unchanged rows are not rewritten

    with pytest.raises(StaleBatchError):
        _run(spark, env, first, "awards")


def test_rejected_row_keeps_previous_version_and_is_quarantined(spark, env, tmp_path):
    first = env["snapshot"]()
    _run(spark, env, first, "awards")

    # The operational database's CHECK constraints make a negative amount impossible at the
    # source, so a bad row is simulated by editing a line of a second snapshot's landed file
    # (local test store only). Silver must still catch rows that violate the rules, whatever
    # later feeds Bronze.
    second = env["snapshot"]()
    manifest = second["awards"]
    path = Path(manifest.files[0].uri.removeprefix("file://"))
    lines = path.read_text().splitlines()
    bad = json.loads(lines[0])
    bad["award_amount"] = -5
    lines[0] = json.dumps(bad)
    path.write_text("\n".join(lines) + "\n")

    run = _run(spark, env, second, "awards")
    assert run.status == "applied" and run.rejected_rows == 1
    kept = spark.sql(
        f"SELECT award_amount FROM {env['ns']}.silver_awards WHERE award_id = '{bad['award_id']}'"
    ).first()
    assert kept is not None and kept.award_amount > 0  # previous good version retained, not deleted
    quarantined = spark.read.json(run.quarantine_uri).collect()
    assert [(q.key_award_id, q.reasons) for q in quarantined] == [
        (bad["award_id"], ["rule:award_amount_positive"])
    ]


def test_quality_gate_blocks_silver_when_too_many_rows_are_rejected(spark, env):
    manifests = env["snapshot"]()
    path = Path(manifests["tenants"].files[0].uri.removeprefix("file://"))
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["tenant_id"] = "Alder!"  # breaks tenant_id_format; tenants allow 0% rejects
    path.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n")

    run = _run(spark, env, manifests, "tenants")
    assert run.status == "blocked" and run.apply is None
    assert not spark.catalog.tableExists(f"{env['ns']}.silver_tenants")


def test_bronze_line_count_must_match_manifest(spark, env):
    manifests = env["snapshot"]()
    path = Path(manifests["agencies"].files[0].uri.removeprefix("file://"))
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    with pytest.raises(BronzeMismatchError):
        _run(spark, env, manifests, "agencies")


def test_select_snapshot_batch_requires_every_table(env):
    manifests = list(env["snapshot"]().values())
    batch = manifests[0].batch_id
    assert select_snapshot_batch(manifests, SPECS) == batch
    with pytest.raises(LookupError):
        select_snapshot_batch([m for m in manifests if m.dataset != "awards"], SPECS)


def test_run_batch_applies_every_table_and_records_a_summary(spark, env, dataset):
    manifests = env["snapshot"]()
    database = env["ns"].split(".", 1)[1]
    batch, runs, key = run_batch(spark, SPECS, env["artifacts"], "lake", database, env["quarantine"])
    assert batch == manifests["awards"].batch_id
    assert {r.table: (r.status, r.apply.rows_after) for r in runs} == {
        t: ("applied", n) for t, n in dataset.counts().items() if t in SPECS
    }
    summary = json.loads(env["artifacts"].get(key))
    assert summary["status"] == "succeeded" and summary["batch_id"] == batch
    assert {t["table"] for t in summary["tables"]} == set(SPECS)

    # Re-running the same batch changes nothing and records a second summary.
    _, again, key2 = run_batch(
        spark, SPECS, env["artifacts"], "lake", database, env["quarantine"], run_id="rerun"
    )
    assert {r.status for r in again} == {"skipped"} and key2 != key


def test_run_batch_refuses_an_incomplete_batch(spark, env):
    manifests = env["snapshot"]()
    database = env["ns"].split(".", 1)[1]
    with pytest.raises(LookupError):
        run_batch(
            spark,
            {**SPECS, "contract_documents_missing": SPECS["awards"]},
            env["artifacts"],
            "lake",
            database,
            env["quarantine"],
            batch_id=manifests["awards"].batch_id,
        )


def test_bench_namespace_is_invisible_to_the_dev_silver_job(spark, env, dataset):
    # ADR 0034: a postgres_bench snapshot lands under its own Bronze/manifest prefixes, so the
    # dev job (source_system="postgres") never picks it up; only the bench job applies it.
    raw = env["artifacts"].root.parent / "raw"
    bench = PostgresSnapshotConnector(
        env["dsn"], LocalLandingStore(raw), env["artifacts"], CONTRACTS, source_system="postgres_bench"
    ).snapshot()
    assert all(m.status == "succeeded" and m.source_system == "postgres_bench" for m in bench)
    assert all(f.key.startswith("bronze/postgres_bench/") for m in bench for f in m.files)

    catalog, database = env["ns"].split(".", 1)
    with pytest.raises(LookupError):
        run_batch(spark, SPECS, env["artifacts"], catalog, database, env["quarantine"])
    batch, runs, summary_key = run_batch(
        spark, SPECS, env["artifacts"], catalog, database, env["quarantine"], source_system="postgres_bench"
    )
    assert batch == bench[0].batch_id and all(r.status == "applied" for r in runs)
    assert summary_key.startswith("pipeline_runs/silver/postgres_bench/")
    assert spark.table(f"{env['ns']}.silver_invoices").count() == dataset.counts()["invoices"]


def test_snapshot_built_tables_carry_the_cdc_lineage_columns_as_null(spark, env):
    # dbt staging reads _is_deleted / _source_sequence from every Silver table (ADR 0034).
    manifests = env["snapshot"]()
    _run(spark, env, manifests, "agencies")
    fq = f"{env['ns']}.silver_agencies"
    fields = {f.name for f in spark.table(fq).schema.fields}
    assert {"_source_commit_ts", "_source_sequence", "_cdc_op", "_is_deleted"} <= fields
    assert spark.sql(f"SELECT count(*) FROM {fq} WHERE _is_deleted IS NOT NULL").collect()[0][0] == 0
