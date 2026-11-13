"""Acceptance test 1: full ingestion. "Load a source dataset → records appear in Bronze and Silver."

The e2e dataset goes from PostgreSQL through the snapshot connector into Bronze, then through
the Silver Glue job's ``run_batch`` into Silver Iceberg. For every table:

- Bronze holds exactly the source rows, byte-for-byte as the manifest recorded (SHA-256);
- Silver holds exactly the source's current rows, with equal values;
- the run summary the data-quality checks read was written.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest
from e2e_support import SPECS, silver_state, source_state
from psycopg import sql

from procurelake.lakehouse.silver.job import run_batch

pytestmark = [pytest.mark.spark, pytest.mark.postgres]


def test_source_dataset_lands_in_bronze_and_silver(spark, source, lake, snapshot):
    manifests = snapshot()
    assert set(manifests) >= set(SPECS) and all(m.status == "succeeded" for m in manifests.values())

    with psycopg.connect(source) as conn:
        counts = {
            t: conn.execute(
                sql.SQL("SELECT count(*) FROM procurement.{}").format(sql.Identifier(t))
            ).fetchone()[0]
            for t in SPECS
        }
    for table, manifest in manifests.items():
        if table not in SPECS:
            continue
        assert manifest.reconciliation.landed_record_count == counts[table], table
        for landed in manifest.files:
            assert hashlib.sha256(lake["raw"].get(landed.key)).hexdigest() == landed.sha256, landed.key

    _batch, runs, summary_key = run_batch(
        spark, SPECS, lake["artifacts"], catalog="lake", database=lake["ns"].split(".")[1],
        quarantine_root=lake["quarantine"],
    )  # fmt: skip
    assert {r.status for r in runs} == {"applied"} and all(r.rejected_rows == 0 for r in runs)
    assert lake["artifacts"].get(summary_key)

    for table, spec in SPECS.items():
        silver = silver_state(spark, f"{lake['ns']}.{spec.silver_table}", spec)
        assert len(silver) == counts[table], table
        assert silver == source_state(source, spec), f"{table}: Silver values differ from PostgreSQL"
    assert sum(counts.values()) == 5_702  # the e2e dataset (ADR 0018)
