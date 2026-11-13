"""Apply validated rows to a Silver Apache Iceberg table.

Snapshot semantics (input is the full current state of the source table):

* **Changed** rows (``_row_hash`` differs) are updated and **new** keys are inserted.
* Keys absent from the snapshot are **deleted**, matching hard deletes at the source.
* Keys present in the snapshot but **rejected** by validation keep their previous Silver version.
  They are neither updated nor deleted, so one bad row never removes good data.

Each table records the last batch applied in table properties. Re-applying the same batch is
skipped, and applying an older batch is refused (batch IDs sort by collection time).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from procurelake.lakehouse.silver.spec import SilverTableSpec

# Identifiers and the batch id are interpolated into Spark SQL (no bind parameters for DDL or
# MERGE), so they are checked against strict patterns first.
_FQ_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){1,2}$")
_BATCH_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _check_identifiers(fq_table: str, batch_id: str | None = None) -> None:
    if not _FQ_TABLE.match(fq_table):
        raise ValueError(f"invalid table identifier {fq_table!r}")
    if batch_id is not None and not _BATCH_ID.match(batch_id):
        raise ValueError(f"invalid batch id {batch_id!r}")


LAST_BATCH_PROPERTY = "procurelake.last_applied_batch_id"
PROVENANCE_DDL = (
    "`_batch_id` string, `_source_file` string, `_source_line_hash` string, `_row_hash` string, "
    "`_silver_updated_at` timestamp"
)

# Change-lineage columns (ADR 0028). Every Silver table has them, whichever path built it, because
# dbt staging reads them (`_is_deleted`, `_source_sequence`). The CDC apply fills them; a snapshot
# apply leaves them null, which staging reads as "not deleted" (ADR 0034: a Silver built only from
# snapshots, like the benchmark one, previously had no such columns and dbt failed on it).
CDC_COLUMNS = [
    ("_source_commit_ts", "timestamp"),
    ("_source_sequence", "string"),
    ("_cdc_op", "string"),
    ("_is_deleted", "boolean"),
]


class StaleBatchError(RuntimeError):
    """Raised when a batch older than the table's last applied batch is applied."""


class SchemaEvolutionError(RuntimeError):
    """The contract changed the table's schema in a way the compatibility rule does not allow."""


class CdcOwnedTableError(RuntimeError):
    """Raised when a snapshot would be applied to a table that CDC now maintains (ADR 0028)."""


@dataclass(frozen=True)
class ApplyResult:
    table: str
    batch_id: str
    skipped: bool
    rows_after: int
    snapshot_id: int | None


def ensure_table(spark: SparkSession, spec: SilverTableSpec, fq_table: str, location: str | None) -> None:
    _check_identifiers(fq_table)
    partition = f"PARTITIONED BY ({', '.join(spec.config.partition_by)})" if spec.config.partition_by else ""
    location_clause = f"LOCATION '{location}'" if location else ""
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {fq_table} ({spec.ddl_schema()}, {PROVENANCE_DDL})
        USING iceberg
        {partition}
        {location_clause}
        TBLPROPERTIES (
            'format-version' = '2',
            'write.merge.mode' = 'copy-on-write',
            'write.update.mode' = 'copy-on-write',
            'write.delete.mode' = 'copy-on-write',
            'procurelake.source_table' = '{spec.source_table}',
            'procurelake.contract_version' = '{spec.contract.contract_version}'
        )
        """
    )
    # Schema evolution (ADR 0022 compatibility rule; acceptance test 6). A column the contract
    # gained since the table was created is added: Iceberg ADD COLUMNS is metadata-only, and
    # existing rows read the new column as null, so no data is rewritten or lost. Within a
    # contract version only nullable additions are allowed, so a new NOT NULL column is refused
    # (its existing rows could not satisfy it). Tables created before the lineage columns were
    # part of every Silver table get those the same way.
    existing = {f.name for f in spark.table(fq_table).schema.fields}
    new_contract = [c for c in spec.contract.columns if c.name not in existing]
    required = [c.name for c in new_contract if not c.nullable]
    if required:
        raise SchemaEvolutionError(
            f"{fq_table}: contract adds NOT NULL column(s) {required}; only nullable columns may be "
            "added within a contract version (ADR 0022)"
        )
    types = dict(spec.columns)
    to_add = [f"`{c.name}` {types[c.name]}" for c in new_contract]
    to_add += [f"`{n}` {t}" for n, t in CDC_COLUMNS if n not in existing]
    if to_add:
        spark.sql(f"ALTER TABLE {fq_table} ADD COLUMNS ({', '.join(to_add)})")
    if new_contract:
        version = spec.contract.contract_version
        spark.sql(f"ALTER TABLE {fq_table} SET TBLPROPERTIES ('procurelake.contract_version' = '{version}')")


def last_applied_batch(spark: SparkSession, fq_table: str) -> str | None:
    rows = spark.sql(f"SHOW TBLPROPERTIES {fq_table}").collect()
    props = {r.key: r.value for r in rows}
    return props.get(LAST_BATCH_PROPERTY)


def apply_snapshot(
    spark: SparkSession,
    spec: SilverTableSpec,
    fq_table: str,
    batch_id: str,
    valid: DataFrame,
    rejected_keys: DataFrame,
) -> ApplyResult:
    _check_identifiers(fq_table, batch_id)
    props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {fq_table}").collect()}
    if props.get("procurelake.cdc_owned") == "true":
        # A snapshot apply hard-deletes absent keys and ignores tombstones and commit order.
        raise CdcOwnedTableError(f"{fq_table} is maintained by CDC; snapshot apply refused")
    previous = props.get(LAST_BATCH_PROPERTY)
    if previous == batch_id:
        return ApplyResult(fq_table, batch_id, True, spark.table(fq_table).count(), None)
    if previous is not None and batch_id < previous:
        raise StaleBatchError(f"{fq_table}: batch {batch_id} is older than last applied batch {previous}")

    key_cols = spec.primary_key
    business = [name for name, _ in spec.columns]
    source = valid.withColumn("_valid", F.lit(True)).withColumn("_silver_updated_at", F.current_timestamp())
    # Keys that exist at the source but were rejected: present in the MERGE source so they are
    # not deleted, flagged invalid so they are not updated or inserted.
    held = rejected_keys.join(valid.select(*key_cols), on=key_cols, how="left_anti").distinct()
    provenance = [
        ("_batch_id", "string"),
        ("_source_file", "string"),
        ("_source_line_hash", "string"),
        ("_row_hash", "string"),
    ]
    for name, stype in [*spec.columns, *provenance]:
        if name not in held.columns:
            held = held.withColumn(name, F.lit(None).cast(stype))
    held = held.withColumn("_valid", F.lit(False)).withColumn("_silver_updated_at", F.current_timestamp())
    merge_source = source.unionByName(held.select(*source.columns))
    view = f"_merge_source_{spec.source_table}"
    merge_source.createOrReplaceTempView(view)

    on = " AND ".join(f"t.`{k}` = s.`{k}`" for k in key_cols)
    all_cols = [
        *business,
        "_batch_id",
        "_source_file",
        "_source_line_hash",
        "_row_hash",
        "_silver_updated_at",
    ]
    set_clause = ", ".join(f"t.`{c}` = s.`{c}`" for c in all_cols)
    insert_cols = ", ".join(f"`{c}`" for c in all_cols)
    insert_vals = ", ".join(f"s.`{c}`" for c in all_cols)
    spark.sql(
        f"""
        MERGE INTO {fq_table} t
        USING {view} s
        ON {on}
        WHEN MATCHED AND s._valid AND t._row_hash <> s._row_hash THEN UPDATE SET {set_clause}
        WHEN NOT MATCHED AND s._valid THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        WHEN NOT MATCHED BY SOURCE THEN DELETE
        """  # noqa: S608 - identifiers validated by _check_identifiers; columns come from the contract
    )
    spark.sql(f"ALTER TABLE {fq_table} SET TBLPROPERTIES ('{LAST_BATCH_PROPERTY}' = '{batch_id}')")
    snapshot = spark.sql(
        f"SELECT snapshot_id FROM {fq_table}.snapshots ORDER BY committed_at DESC LIMIT 1"  # noqa: S608
    ).collect()
    return ApplyResult(
        fq_table,
        batch_id,
        False,
        spark.table(fq_table).count(),
        snapshot[0].snapshot_id if snapshot else None,
    )
