"""Apply AWS DMS change events from Bronze to a Silver Iceberg table (C3, ADR 0028).

Input: DMS Parquet files for one table and one ``load_id`` (ADR 0027): ``LOAD*.parquet``
(full load, ``Op = I``, ``_dms_commit_ts`` = task start) and timestamp-named CDC files
(``Op`` I/U/D, ``_dms_commit_ts`` = source commit time). A delete carries only the primary key.

Rules:

* **Order.** Each event gets a sortable ``_source_sequence``: commit timestamp (microseconds),
  then full-load files before CDC files, then file name, then row position in the file
  (``_metadata.row_index``). DMS writes each table's changes in commit order but does not
  preserve order across tables (DMS docs), so nothing here relies on cross-table order.
* **Validation.** Inserts/updates are checked with the same contract checks and rules as the
  snapshot path (``transform.with_reasons``); deletes need only the primary key. Rejected events
  go to quarantine with their reasons. If a key's **latest** event is rejected, the key is held:
  its previous Silver version is kept (no older event is applied in its place).
* **Quality gate.** Rejected / total events above the table's ``max_reject_ratio`` blocks the
  apply: nothing is written, the checkpoint does not move.
* **Apply.** One ``MERGE``: only an event whose sequence is greater than the row's stored
  ``_source_sequence`` changes it, so replaying any input is a no-op and an older event can never
  overwrite a newer one. A delete becomes a **tombstone** (``_is_deleted = true``, business
  columns kept); without tombstones a replayed older insert could resurrect a deleted row.
* **Complete full load.** When the batch contains a complete full load, keys absent from it whose
  stored sequence is older than the load are tombstoned too (the load is a snapshot of the table).
* **Post-apply validation** before the checkpoint may advance: every applied key's Silver
  sequence is at least the event's, and the table has no duplicate keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from procurelake.lakehouse.silver.spec import SilverTableSpec
from procurelake.lakehouse.silver.transform import with_reasons
from procurelake.lakehouse.silver.writer import LAST_BATCH_PROPERTY, _check_identifiers, ensure_table

CDC_OWNED_PROPERTY = "procurelake.cdc_owned"
COMMIT_COLUMN = "_dms_commit_ts"
CDC_COLUMNS = [
    ("_source_commit_ts", "timestamp"),
    ("_source_sequence", "string"),
    ("_cdc_op", "string"),
    ("_is_deleted", "boolean"),
]
_LOAD_ID = re.compile(r"/load_id=([0-9TZ]+)/")


class CdcApplyError(RuntimeError):
    """Post-apply validation failed: the checkpoint must not advance."""


@dataclass(frozen=True)
class CdcApplyResult:
    table: str
    batch_id: str
    status: Literal["applied", "blocked", "empty"]
    files: int
    events: int
    rejected_events: int
    held_keys: int
    applied_keys: int
    full_load_tombstones: int
    last_sequence: str | None
    quarantine_uri: str | None


def load_id_of(uri: str) -> str:
    match = _LOAD_ID.search(uri)
    if not match:
        raise ValueError(f"no load_id in {uri!r}")
    return match.group(1)


def is_full_load_file(uri: str) -> bool:
    return uri.rsplit("/", 1)[-1].startswith("LOAD")


def order_files(uris: list[str]) -> list[str]:
    """Apply order within a load_id: full-load files, then CDC files by (timestamped) name."""
    return sorted(uris, key=lambda u: (load_id_of(u), 0 if is_full_load_file(u) else 1, u.rsplit("/", 1)[-1]))


def read_events(spark: SparkSession, spec: SilverTableSpec, uris: list[str]) -> DataFrame:
    """DMS Parquet -> typed business columns + _cdc_op, _source_commit_ts, _source_sequence, ..."""
    raw = spark.read.parquet(*uris)
    missing = [n for n, _ in spec.columns if n not in raw.columns]
    if missing or "Op" not in raw.columns or COMMIT_COLUMN not in raw.columns:
        raise ValueError(f"{spec.source_table}: DMS files lack columns {missing or ['Op', COMMIT_COLUMN]}")
    file_path = F.col("_metadata.file_path")
    file_name = F.element_at(F.split(file_path, "/"), -1)
    commit_ts = F.to_timestamp(F.col(COMMIT_COLUMN), "yyyy-MM-dd HH:mm:ss.SSSSSS")
    events = raw.select(
        *[F.col(n).cast(t).alias(n) for n, t in spec.columns],
        F.col("Op").alias("_cdc_op"),
        commit_ts.alias("_source_commit_ts"),
        file_path.alias("_source_file"),
        F.col("_metadata.row_index").alias("_row_index"),
        F.sha2(F.to_json(F.struct(*[F.col(c) for c in raw.columns])), 256).alias("_source_line_hash"),
        file_name.alias("_file_name"),
    )
    return events.withColumn(
        "_source_sequence",
        F.concat_ws(
            "|",
            F.date_format("_source_commit_ts", "yyyyMMddHHmmssSSSSSS"),
            F.when(F.col("_file_name").startswith("LOAD"), F.lit("0")).otherwise(F.lit("1")),
            F.col("_file_name"),
            F.lpad(F.col("_row_index").cast("string"), 12, "0"),
        ),
    ).drop("_file_name")


def _event_reasons(events: DataFrame, spec: SilverTableSpec) -> DataFrame:
    checked = with_reasons(events, spec, F.lit(True), lambda name: F.col(name).isNotNull())
    pk_missing = F.filter(
        F.array(*[F.when(F.col(k).isNull(), F.lit(f"not_null:{k}")) for k in spec.primary_key]),
        lambda r: r.isNotNull(),
    )
    bad_op = F.when(~F.col("_cdc_op").isin("I", "U", "D"), F.array(F.lit("unknown_op")))
    return checked.withColumn(
        "reasons",
        F.coalesce(
            bad_op,
            F.when(F.col("_cdc_op") == "D", pk_missing).otherwise(F.col("reasons")),
        ),
    )


def ensure_cdc_table(spark: SparkSession, spec: SilverTableSpec, fq_table: str, location: str | None) -> None:
    """Create the Silver table if needed, add the CDC columns, and mark it CDC-owned."""
    ensure_table(spark, spec, fq_table, location)
    existing = {f.name for f in spark.table(fq_table).schema.fields}
    to_add = [f"`{n}` {t}" for n, t in CDC_COLUMNS if n not in existing]
    if to_add:
        spark.sql(f"ALTER TABLE {fq_table} ADD COLUMNS ({', '.join(to_add)})")
    spark.sql(f"ALTER TABLE {fq_table} SET TBLPROPERTIES ('{CDC_OWNED_PROPERTY}' = 'true')")


def apply_events(
    spark: SparkSession,
    spec: SilverTableSpec,
    fq_table: str,
    uris: list[str],
    batch_id: str,
    quarantine_root: str,
    table_location: str | None = None,
    full_load_complete: bool = False,
) -> CdcApplyResult:
    """Apply the DMS files ``uris`` (one load_id) to ``fq_table``. See module docstring."""
    _check_identifiers(fq_table, batch_id)
    uris = order_files(uris)
    if not uris:
        return CdcApplyResult(spec.silver_table, batch_id, "empty", 0, 0, 0, 0, 0, 0, None, None)
    load_ids = {load_id_of(u) for u in uris}
    if len(load_ids) != 1:
        raise ValueError(f"one apply must cover one load_id, got {sorted(load_ids)}")
    has_full_load = any(is_full_load_file(u) for u in uris)
    if full_load_complete and not has_full_load:
        raise ValueError("full_load_complete=True needs the full-load files in the batch")

    events = _event_reasons(read_events(spark, spec, uris), spec).cache()
    total = events.count()
    rejected = events.where(F.size("reasons") > 0)
    rejected_count = rejected.count()

    quarantine_uri = None
    if rejected_count:
        quarantine_uri = f"{quarantine_root.rstrip('/')}/silver_cdc/{spec.source_table}/batch_id={batch_id}/"
        rejected.select(
            F.lit(spec.source_table).alias("source_table"),
            F.lit(batch_id).alias("batch_id"),
            F.col("_source_file").alias("source_file"),
            F.col("_source_sequence").alias("source_sequence"),
            F.col("_cdc_op").alias("op"),
            F.to_json(F.struct(*[F.col(n) for n, _ in spec.columns])).alias("event"),
            F.col("reasons"),
            *[F.col(k).cast("string").alias(f"key_{k}") for k in spec.primary_key],
        ).coalesce(1).write.mode("errorifexists").json(quarantine_uri)

    ratio = rejected_count / total if total else 0.0
    if ratio > spec.config.max_reject_ratio:
        events.unpersist()
        return CdcApplyResult(
            spec.silver_table,
            batch_id,
            "blocked",
            len(uris),
            total,
            rejected_count,
            0,
            0,
            0,
            None,
            quarantine_uri,
        )

    # Latest event per key (all events, valid or not). A key whose latest event is rejected is
    # held at its previous Silver version.
    key_window = Window.partitionBy(*spec.primary_key).orderBy(F.col("_source_sequence").desc())
    carry = Window.partitionBy(*spec.primary_key)
    latest = (
        events.withColumn("_rank", F.row_number().over(key_window))
        # Deletes carry only the key: keep the tenant from any other event for the same key.
        .withColumn("_tenant_carry", F.max("tenant_id").over(carry))
        .where(F.col("_rank") == 1)
    )
    held = latest.where(F.size("reasons") > 0).count()
    business = [n for n, _ in spec.columns]
    # Held keys stay in the MERGE source flagged _hold, so they are neither changed nor (with a
    # complete full load) mistaken for keys missing from the load.
    source = latest.select(
        *[
            (F.coalesce(F.col(n), F.col("_tenant_carry")) if n == "tenant_id" else F.col(n)).alias(n)
            for n in business
        ],
        F.lit(batch_id).alias("_batch_id"),
        F.col("_source_file"),
        F.col("_source_line_hash"),
        F.when(
            F.col("_cdc_op") != "D", F.sha2(F.to_json(F.struct(*[F.col(c) for c in business])), 256)
        ).alias("_row_hash"),
        F.current_timestamp().alias("_silver_updated_at"),
        F.col("_source_commit_ts"),
        F.col("_source_sequence"),
        F.col("_cdc_op"),
        (F.col("_cdc_op") == "D").alias("_is_deleted"),
        (F.size("reasons") > 0).alias("_hold"),
    )
    applied = source.where(~F.col("_hold"))
    applied_keys = applied.count()
    last_sequence = applied.agg(F.max("_source_sequence")).first()[0]

    ensure_cdc_table(spark, spec, fq_table, table_location)
    view = f"_cdc_source_{spec.source_table}"
    source.createOrReplaceTempView(view)
    on = " AND ".join(f"t.`{k}` = s.`{k}`" for k in spec.primary_key)
    newer = "(t._source_sequence IS NULL OR s._source_sequence > t._source_sequence)"
    meta = ["_batch_id", "_source_file", "_source_line_hash", "_silver_updated_at",
            "_source_commit_ts", "_source_sequence", "_cdc_op", "_is_deleted"]  # fmt: skip
    all_cols = [*business, "_row_hash", *meta]
    tombstone_set = ", ".join(f"t.`{c}` = s.`{c}`" for c in meta)
    update_set = ", ".join(f"t.`{c}` = s.`{c}`" for c in all_cols)
    insert_cols = ", ".join(f"`{c}`" for c in all_cols)
    insert_vals = ", ".join(f"s.`{c}`" for c in all_cols)
    full_load_clause = ""
    if full_load_complete:
        load_start = (
            applied.where(F.col("_source_file").contains("/LOAD")).agg(F.min("_source_sequence")).first()[0]
        )
        if load_start is None:
            raise CdcApplyError(f"{fq_table}: full load in batch {batch_id} has no valid rows")
        # Keys absent from a complete full load, and older than it, no longer exist at the source.
        full_load_clause = f"""
        WHEN NOT MATCHED BY SOURCE AND NOT coalesce(t._is_deleted, false)
             AND (t._source_sequence IS NULL OR t._source_sequence < '{_sql_str(load_start)}')
        THEN UPDATE SET t._is_deleted = true, t._cdc_op = 'D', t._batch_id = '{batch_id}',
             t._source_sequence = '{_sql_str(load_start)}', t._silver_updated_at = current_timestamp()
        """
    before_tombstones = _tombstones(spark, fq_table)
    spark.sql(
        f"""
        MERGE INTO {fq_table} t
        USING {view} s
        ON {on}
        WHEN MATCHED AND NOT s._hold AND {newer} AND s._is_deleted THEN UPDATE SET {tombstone_set}
        WHEN MATCHED AND NOT s._hold AND {newer} THEN UPDATE SET {update_set}
        WHEN NOT MATCHED AND NOT s._hold THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        {full_load_clause}
        """  # noqa: S608 - identifiers validated; columns come from the contract
    )
    full_load_tombstones = 0
    if full_load_complete:
        deleted_now = _tombstones(spark, fq_table) - before_tombstones
        deleted_by_events = applied.where("_is_deleted").count()
        full_load_tombstones = max(0, deleted_now - deleted_by_events)

    _validate_applied(spark, fq_table, view, spec)  # applied (non-held) keys only
    spark.sql(f"ALTER TABLE {fq_table} SET TBLPROPERTIES ('{LAST_BATCH_PROPERTY}' = '{batch_id}')")
    events.unpersist()
    return CdcApplyResult(
        spec.silver_table,
        batch_id,
        "applied",
        len(uris),
        total,
        rejected_count,
        held,
        applied_keys,
        full_load_tombstones,
        last_sequence,
        quarantine_uri,
    )


def _sql_str(value: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z|._-]+", value):
        raise ValueError(f"unexpected sequence value {value!r}")
    return value


def _tombstones(spark: SparkSession, fq_table: str) -> int:
    return spark.table(fq_table).where("coalesce(_is_deleted, false)").count()


def _validate_applied(spark: SparkSession, fq_table: str, view: str, spec: SilverTableSpec) -> None:
    on = " AND ".join(f"t.`{k}` = s.`{k}`" for k in spec.primary_key)
    behind = spark.sql(
        f"SELECT count(*) AS n FROM {view} s LEFT JOIN {fq_table} t ON {on} "  # noqa: S608
        "WHERE NOT s._hold AND (t._source_sequence IS NULL OR t._source_sequence < s._source_sequence)"
    ).first()["n"]
    keys = ", ".join(f"`{k}`" for k in spec.primary_key)
    duplicates = spark.sql(
        f"SELECT count(*) AS n FROM (SELECT {keys} FROM {fq_table} GROUP BY {keys} HAVING count(*) > 1)"  # noqa: S608
    ).first()["n"]
    if behind or duplicates:
        raise CdcApplyError(
            f"{fq_table}: {behind} applied keys behind their event, {duplicates} duplicate keys"
        )


def active_rows(table: DataFrame) -> DataFrame:
    """Current-state rows (tombstones removed)."""
    return table.where(~F.coalesce(F.col("_is_deleted"), F.lit(False)))


def is_cdc_owned(spark: SparkSession, fq_table: str) -> bool:
    if not spark.catalog.tableExists(fq_table):
        return False
    props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {fq_table}").collect()}
    return props.get(CDC_OWNED_PROPERTY) == "true"
