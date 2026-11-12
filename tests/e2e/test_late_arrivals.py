"""Acceptance test 5: late arrivals. "Deliver an older event after a newer one → the correct
final state is kept."

Two real updates to one supplier, committed in order in PostgreSQL. The file holding the newer
event is delivered and applied first; the file holding the older event arrives later (a
later file name, an earlier commit time) and is applied in a second run. Silver keeps the
newer value, as PostgreSQL does, because the merge orders by source commit time and
sequence, not by arrival (ADR 0028).
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from cdc_flow import full_load
from e2e_support import SPECS, comparable_columns, silver_state, source_state
from psycopg.rows import dict_row

from procurelake.data_generator.workload import Change

pytestmark = [pytest.mark.spark, pytest.mark.postgres]


def _rename(source: str, supplier_id: str, name: str) -> Change:
    with psycopg.connect(source) as conn, conn.cursor(row_factory=dict_row) as cur:
        after = cur.execute(
            "UPDATE procurement.suppliers SET supplier_name = %s WHERE supplier_id = %s RETURNING *",
            (name, supplier_id),
        ).fetchone()
        committed = cur.execute("SELECT clock_timestamp() AS ts").fetchone()["ts"]
    return Change("rename_supplier", "suppliers", "U", supplier_id, after["tenant_id"], after, 0, committed)


def test_an_older_event_arriving_late_does_not_overwrite_the_newer_state(spark, source, lake, dms, cdc_job):
    spec = SPECS["suppliers"]
    full_load(dms, source)
    with psycopg.connect(source) as conn:
        supplier_id = str(
            conn.execute("SELECT min(supplier_id::text) FROM procurement.suppliers").fetchone()[0]
        )

    older = _rename(source, supplier_id, "Late Arrival Test Ltd (v1)")
    newer = _rename(source, supplier_id, "Late Arrival Test Ltd (v2)")
    assert older.committed_at < newer.committed_at

    dms.changes(spec, [newer], name="20261112-130000000.parquet")
    cdc_job(spec)
    dms.changes(spec, [older], name=datetime.now(UTC).strftime("%Y%m%d-%H%M%S000") + ".parquet")
    [late] = cdc_job(spec)
    assert late.status == "applied"

    silver = silver_state(spark, f"{lake['ns']}.{spec.silver_table}", spec)
    assert (
        silver[supplier_id][comparable_columns(spec).index("supplier_name")] == "Late Arrival Test Ltd (v2)"
    )
    assert silver == source_state(source, spec)
