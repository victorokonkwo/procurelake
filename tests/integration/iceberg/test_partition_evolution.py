"""Partition evolution on a local Iceberg table (Spark 3.5 + Iceberg 1.10.0, as on Glue 5.1).

Checks what ADR 0034 relies on: ADD PARTITION FIELD is metadata-only (existing files keep spec
0), new writes use the new spec, rewrite_data_files with rewrite-all moves every file to the new
spec, rows are unchanged throughout, and a rerun is idempotent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from procurelake.lakehouse.partitioning import PartitionField, evolve, layout

pytestmark = pytest.mark.spark


@pytest.fixture
def table(spark, tmp_path):
    ns = f"lake.pe_{tmp_path.name.replace('-', '_')[-20:]}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    fq = f"{ns}.silver_invoices"
    spark.sql(f"CREATE TABLE {fq} (invoice_id string, invoice_date date, amount decimal(18,2)) USING iceberg")
    for month in range(1, 7):  # six small commits, one per month
        spark.sql(
            f"INSERT INTO {fq} SELECT concat('i{month}_', cast(id AS string)), "
            f"date_add(DATE '2025-{month:02d}-01', cast(id % 27 AS int)), CAST(id AS decimal(18,2)) "
            "FROM range(50)"
        )
    return ns.split(".", 1)[1], fq


def _rows(spark, fq):
    return sorted(tuple(r) for r in spark.table(fq).collect())


def test_add_field_is_metadata_only_then_rewrite_moves_every_file(spark, table):
    database, fq = table
    rows = _rows(spark, fq)
    field = PartitionField("silver_invoices", "months", "invoice_date")

    added = evolve(spark, "lake", database, field, rewrite=False)
    assert added.added and added.before.spec == [] and added.after.spec == ["months(invoice_date)"]
    assert set(added.after.files_by_spec) == {0}  # nothing rewritten yet
    assert added.after.data_files == added.before.data_files

    spark.sql(f"INSERT INTO {fq} VALUES ('new', DATE '2025-06-15', 1.00)")
    rows.append(("new", date(2025, 6, 15), Decimal("1.00")))
    assert set(layout(spark, fq).files_by_spec) == {0, 1}  # new writes use the new spec

    rewritten = evolve(spark, "lake", database, field, rewrite=True)
    assert not rewritten.added  # idempotent: already in the spec
    assert set(rewritten.after.files_by_spec) == {1}
    assert rewritten.after.partitions == 6
    assert rewritten.rewrite["rewritten_data_files_count"] == rewritten.before.data_files
    assert _rows(spark, fq) == sorted(rows)
