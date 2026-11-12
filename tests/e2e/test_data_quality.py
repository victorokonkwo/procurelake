"""Acceptance test 7: data quality. "Introduce invalid supplier data → validation blocks or
quarantines it."

PostgreSQL's own CHECK constraints reject a blank supplier name or category, so invalid supplier
data cannot come from the operational database. It arrives through a faulty extract. Here a
second snapshot's landed supplier file is edited (local test store only) before the Silver job
reads it, then the daily data-quality checks (ADR 0040) read the resulting run summary:

- **one invalid row** (under the 1% limit): quarantined with the rule that failed; its previous
  Silver version is kept, not deleted; the reconciliation checks pass (the quarantine holds
  exactly the rejected row);
- **invalid rows above the limit:** the table is blocked and Silver is not touched; the checks
  report the blocked table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from e2e_support import SPECS, silver_state

from procurelake.lakehouse.silver.job import run_batch
from procurelake.quality.checks import latest_silver_summary, manifests_by_batch, summary_checks

pytestmark = [pytest.mark.spark, pytest.mark.postgres]


def _blank_names(manifest, how_many: int) -> list[str]:
    """Blank the supplier_name of the first ``how_many`` landed lines; returns their keys."""
    path = Path(manifest.files[0].uri.removeprefix("file://"))
    lines = path.read_text().splitlines()
    keys = []
    for i in range(how_many):
        row = json.loads(lines[i])
        row["supplier_name"] = "   "
        lines[i] = json.dumps(row)
        keys.append(row["supplier_id"])
    path.write_text("\n".join(lines) + "\n")
    return keys


def _quarantine_lines(uri: str) -> int:
    root = Path(uri.removeprefix("file://"))
    return sum(1 for f in root.rglob("*.json") for line in f.read_text().splitlines() if line.strip())


def _checks(lake):
    _, summary = latest_silver_summary(lake["artifacts"], "postgres")
    manifests = manifests_by_batch(lake["artifacts"], "postgres", SPECS)
    return {
        (c.name, c.subject.split("/")[1].split()[0]): c
        for c in summary_checks(summary, manifests, _quarantine_lines, "postgres")
    }


def _silver(lake):
    return lake["ns"].split(".")[1], f"{lake['ns']}.silver_suppliers"


def test_one_invalid_supplier_is_quarantined_and_the_good_version_kept(spark, lake, snapshot):
    database, table = _silver(lake)
    snapshot()
    run_batch(spark, SPECS, lake["artifacts"], "lake", database, lake["quarantine"], run_id="r1")
    before = silver_state(spark, table, SPECS["suppliers"])

    second = snapshot()
    [bad_key] = _blank_names(second["suppliers"], 1)
    _, runs, _ = run_batch(spark, SPECS, lake["artifacts"], "lake", database, lake["quarantine"], run_id="r2")
    suppliers = next(r for r in runs if r.table == "suppliers")

    assert suppliers.status == "applied" and suppliers.rejected_rows == 1
    quarantined = spark.read.json(suppliers.quarantine_uri).collect()
    assert [(q.key_supplier_id, q.reasons) for q in quarantined] == [
        (bad_key, ["rule:supplier_name_not_blank"])
    ]
    assert (
        silver_state(spark, table, SPECS["suppliers"])[bad_key] == before[bad_key]
    )  # not blanked, not deleted

    checks = _checks(lake)
    assert checks[("quarantine_holds_every_rejected_row", "suppliers")].passed
    assert all(c.passed for c in checks.values()), [c for c in checks.values() if not c.passed]


def test_invalid_suppliers_above_the_limit_block_the_table(spark, lake, snapshot):
    database, table = _silver(lake)
    snapshot()
    run_batch(spark, SPECS, lake["artifacts"], "lake", database, lake["quarantine"], run_id="r1")
    before = silver_state(spark, table, SPECS["suppliers"])
    snapshots_before = spark.table(f"{table}.snapshots").count()

    second = snapshot()
    _blank_names(second["suppliers"], 5)  # 5 of 105 rows: ~4.8%, over the 1% limit
    _, runs, _ = run_batch(spark, SPECS, lake["artifacts"], "lake", database, lake["quarantine"], run_id="r2")
    suppliers = next(r for r in runs if r.table == "suppliers")

    assert suppliers.status == "blocked" and suppliers.rejected_rows == 5
    assert silver_state(spark, table, SPECS["suppliers"]) == before
    assert spark.table(f"{table}.snapshots").count() == snapshots_before

    blocked = _checks(lake)[("table_not_blocked", "suppliers")]
    assert not blocked.passed and "blocked" in blocked.detail
