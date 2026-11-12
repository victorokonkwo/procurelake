"""Acceptance test 4: replay. "Process the same input twice → no duplicate business records."

Both Silver paths, end to end from PostgreSQL:

- **snapshot:** the same Bronze batch through the Silver job twice. The second run is skipped
  by the batch recorded on each table, and nothing changes;
- **CDC:** a full load plus real committed changes through the CDC job, then the same change
  files delivered again under new names (DMS re-sending them after a task restart) and the job
  run again. No business key appears twice, and Silver still equals PostgreSQL.
"""

from __future__ import annotations

import pytest
from cdc_flow import CDC_TABLES, commit_workload, full_load
from e2e_support import SPECS, duplicate_keys, silver_state, source_state

from procurelake.lakehouse.silver.job import run_batch

pytestmark = [pytest.mark.spark, pytest.mark.postgres]


def test_snapshot_batch_processed_twice_changes_nothing(spark, lake, snapshot):
    snapshot()
    database = lake["ns"].split(".")[1]
    first_batch, _, _ = run_batch(spark, SPECS, lake["artifacts"], "lake", database, lake["quarantine"])
    before = {t: silver_state(spark, f"{lake['ns']}.{s.silver_table}", s) for t, s in SPECS.items()}

    again_batch, runs, _ = run_batch(
        spark,
        SPECS,
        lake["artifacts"],
        "lake",
        database,
        lake["quarantine"],
        batch_id=first_batch,
        run_id="replay",
    )
    assert again_batch == first_batch
    assert {r.status for r in runs} == {"skipped"}
    for table, spec in SPECS.items():
        fq = f"{lake['ns']}.{spec.silver_table}"
        assert silver_state(spark, fq, spec) == before[table], table
        assert duplicate_keys(spark, fq, spec) == 0, table


def test_cdc_input_processed_twice_changes_nothing(spark, source, lake, dms, cdc_job):
    full_load(dms, source)
    workload = commit_workload(source, seed=7, operations=60)
    assert len(workload.changes) > 20
    delivered = {t: dms.changes(SPECS[t], workload.changes) for t in CDC_TABLES}
    for table in CDC_TABLES:
        cdc_job(SPECS[table])
    first = {t: silver_state(spark, f"{lake['ns']}.{SPECS[t].silver_table}", SPECS[t]) for t in CDC_TABLES}

    # The same events again, in new files, and a run with nothing new at all.
    for table in CDC_TABLES:
        assert cdc_job(SPECS[table]) == []
        if delivered[table]:
            dms.changes(SPECS[table], workload.changes, name="20991231-235959999.parquet")
            [result] = cdc_job(SPECS[table])
            assert result.status == "applied"

    for table in CDC_TABLES:
        spec, fq = SPECS[table], f"{lake['ns']}.{SPECS[table].silver_table}"
        assert duplicate_keys(spark, fq, spec) == 0, table
        assert silver_state(spark, fq, spec) == first[table] == source_state(source, spec), table
