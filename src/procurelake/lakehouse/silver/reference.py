"""Windowed reference batches (USAspending) -> one current-state Silver Iceberg table (ADR 0044).

The operational tables arrive as full snapshots (``pipeline.py``): absent keys are deleted. A
USAspending batch is different. It is one day of records by ``Last Modified Date``, so an award
appears again, in a later batch, each time it changes; and a re-sweep (open question 30)
re-collects days already applied. So Silver is an **upsert, never a delete**:

1. **Which batches:** every succeeded, settled manifest of the contract's dataset and Bronze schema
   version that no earlier run applied. The applied list is the run summaries under
   ``pipeline_runs/silver/<source_system>/``, written only after the MERGE and its validation
   (checkpoint after the write, AGENTS.md section 2.5). A crash before the summary re-applies the
   same batches next time, which is safe because the MERGE is idempotent.
2. **Read** exactly the page files each manifest lists; the record count must equal the
   manifest's landed count.
3. **Validate** every record against the contract (type, not-null). Rejected records go to
   ``<quarantine_root>/silver/<dataset>/batch_id=<id>/`` with reasons. A batch whose reject ratio
   exceeds ``max_reject_ratio`` is blocked: not applied, not marked applied, and the job fails.
4. **Deduplicate** across the batches of this run: per key, the latest ``Last Modified Date``
   wins; a tie goes to the later batch (batch IDs sort by collection time).
5. **MERGE** with an order guard: a row is replaced only by a strictly newer
   ``Last Modified Date``, or by the same one from a later batch with different content. An
   older or identical record never changes Silver, so re-applying or re-sweeping is safe.
6. **Report** per batch what it found against Silver before the MERGE: new keys, changed keys,
   unchanged, and older than Silver (the re-sweep measurement, open question 30).

Source deletes are not handled: the API publishes current records only (``deletes: none``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from procurelake.contracts.reference import ReferenceContract
from procurelake.ingestion.manifest import IngestionManifest

logger = logging.getLogger(__name__)

RUNS_ROOT = "pipeline_runs/silver"
_FQ_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){1,2}$")
PROVENANCE_DDL = (
    "`currency_code` string, `_batch_id` string, `_window_date` date, `_source_file` string, "
    "`_source_position` int, `_row_hash` string, `_silver_updated_at` timestamp"
)


class BronzeMismatchError(RuntimeError):
    """Bronze page files do not hold the records their manifest recorded."""


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    window_date: str
    purpose: str
    status: str  # applied | blocked
    input_records: int
    valid_records: int
    rejected_records: int
    reject_ratio: float
    quarantine_uri: str | None
    new_keys: int = 0
    changed_keys: int = 0
    unchanged_keys: int = 0
    older_than_silver: int = 0
    superseded_in_run: int = 0


@dataclass
class ReferenceRun:
    table: str
    batches: list[BatchResult] = field(default_factory=list)
    rows_after: int | None = None
    snapshot_id: int | None = None

    @property
    def applied(self) -> list[str]:
        return [b.batch_id for b in self.batches if b.status == "applied"]

    @property
    def blocked(self) -> list[str]:
        return [b.batch_id for b in self.batches if b.status == "blocked"]

    def as_dict(self) -> dict:
        return {
            "table": self.table,
            "rows_after": self.rows_after,
            "snapshot_id": self.snapshot_id,
            "applied_batch_ids": self.applied,
            "blocked_batch_ids": self.blocked,
            "batches": [asdict(b) for b in self.batches],
        }


def pending_batches(
    manifests: Iterable[IngestionManifest], contract: ReferenceContract, applied: set[str]
) -> list[IngestionManifest]:
    """Succeeded, settled batches of the contract's dataset not applied yet, oldest first."""
    out = [
        m
        for m in manifests
        if m.status == "succeeded"
        and m.window_settled
        and m.window is not None
        and m.source_system == contract.source_system
        and m.dataset == contract.dataset
        and m.schema_version == contract.bronze_schema_version
        and m.batch_id not in applied
    ]
    return sorted(out, key=lambda m: m.batch_id)


def _parse_schema(contract: ReferenceContract) -> str:
    """DDL schema to parse one record: nested fields as structs, money as wide decimals."""
    top: dict[str, list[str] | str] = {}
    for c in contract.columns:
        head, _, sub = c.source_field.partition(".")
        stype = "decimal(38,10)" if c.scale is not None else "string"
        if sub:
            top.setdefault(head, [])
            top[head].append(f"`{sub}`: string")  # type: ignore[union-attr]
        else:
            top[head] = stype
    parts = []
    for name, stype in top.items():
        parts.append(
            f"`{name}` struct<{', '.join(stype)}>" if isinstance(stype, list) else f"`{name}` {stype}"
        )
    return ", ".join(parts)


def _source_col(field_path: str) -> Column:
    head, _, sub = field_path.partition(".")
    col = F.col(f"_parsed.`{head}`")
    return col.getField(sub) if sub else col


def _present(field_path: str) -> Column:
    """The record supplied a non-null value (before type conversion)."""
    head, _, sub = field_path.partition(".")
    if sub:
        return _source_col(field_path).isNotNull()
    return F.col("_present")[head].isNotNull() & (F.col("_present")[head] != F.lit(""))


def _typed(c) -> Column:
    raw = _source_col(c.source_field)
    if c.type == "date":
        return F.to_date(raw, "yyyy-MM-dd")
    if c.type == "timestamp_ntz":
        return F.to_timestamp_ntz(raw, F.lit("yyyy-MM-dd HH:mm:ss"))
    if c.scale is not None:
        # Exact: the JSON number is parsed into a wide decimal, then must fit the declared scale
        # without rounding; otherwise it is a type mismatch, never a silently rounded amount.
        narrowed = raw.cast(c.spark_type)
        return F.when(narrowed == raw, narrowed)
    return F.when(raw != F.lit(""), raw)


def read_records(spark: SparkSession, manifest: IngestionManifest) -> DataFrame:
    """One row per record of the batch's page files: its JSON text, file and position."""
    paths = [f.uri for f in manifest.files]
    if not paths:
        return spark.createDataFrame([], "_record string, _source_file string, _source_position int")
    pages = spark.read.option("wholetext", "true").text(paths)
    return pages.select(
        F.input_file_name().alias("_source_file"),
        F.posexplode(F.from_json("value", "struct<results: array<string>>").getField("results")).alias(
            "_source_position", "_record"
        ),
    ).select("_record", "_source_file", F.col("_source_position").cast("int"))


def validate(records: DataFrame, contract: ReferenceContract, manifest: IngestionManifest) -> DataFrame:
    """Typed rows with a ``reasons`` array (empty = valid) and provenance columns."""
    parsed = records.withColumn("_parsed", F.from_json("_record", _parse_schema(contract))).withColumn(
        "_present", F.from_json("_record", "map<string,string>")
    )
    is_object = F.col("_parsed").isNotNull() & F.col("_present").isNotNull()
    for c in contract.columns:
        parsed = parsed.withColumn(c.name, _typed(c))
    checks = [F.when(~is_object, F.lit("unparseable_json"))]
    for c in contract.columns:
        checks.append(
            F.when(
                is_object & _present(c.source_field) & F.col(c.name).isNull(),
                F.lit(f"type_mismatch:{c.name}"),
            )
        )
        if not c.nullable:
            checks.append(F.when(is_object & ~_present(c.source_field), F.lit(f"not_null:{c.name}")))
    business = [c.name for c in contract.columns]
    window = manifest.window.start_date.isoformat() if manifest.window else None
    return parsed.select(
        *business,
        F.filter(F.array(*checks), lambda r: r.isNotNull()).alias("reasons"),
        F.lit(contract.currency_code).alias("currency_code"),
        F.lit(manifest.batch_id).alias("_batch_id"),
        F.lit(window).cast("date").alias("_window_date"),
        "_source_file",
        "_source_position",
        F.sha2(F.to_json(F.struct(*[F.col(n) for n in business])), 256).alias("_row_hash"),
        "_record",
    )


def ensure_table(
    spark: SparkSession, contract: ReferenceContract, fq_table: str, location: str | None
) -> None:
    if not _FQ_TABLE.match(fq_table):
        raise ValueError(f"invalid table identifier {fq_table!r}")
    columns = ", ".join(f"`{c.name}` {c.spark_type}" for c in contract.columns)
    location_clause = f"LOCATION '{location}'" if location else ""
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {fq_table} ({columns}, {PROVENANCE_DDL})
        USING iceberg
        {location_clause}
        TBLPROPERTIES (
            'format-version' = '2',
            'write.merge.mode' = 'copy-on-write',
            'write.update.mode' = 'copy-on-write',
            'procurelake.source_table' = '{contract.name}',
            'procurelake.contract_version' = '{contract.contract_version}'
        )
        """
    )
    existing = {f.name for f in spark.table(fq_table).schema.fields}
    new = [c for c in contract.columns if c.name not in existing]
    required = [c.name for c in new if not c.nullable]
    if required:
        raise ValueError(
            f"{fq_table}: contract adds NOT NULL column(s) {required}; only nullable additions allowed"
        )
    if new:
        spark.sql(
            f"ALTER TABLE {fq_table} ADD COLUMNS ({', '.join(f'`{c.name}` {c.spark_type}' for c in new)})"
        )


def _purpose(manifest: IngestionManifest) -> str:
    return str(manifest.request.get("purpose", "daily"))


def apply_batches(
    spark: SparkSession,
    contract: ReferenceContract,
    manifests: list[IngestionManifest],
    fq_table: str,
    quarantine_root: str,
    location: str | None = None,
) -> ReferenceRun:
    """Validate, deduplicate and MERGE the given batches (see the module docstring)."""
    run = ReferenceRun(fq_table)
    ensure_table(spark, contract, fq_table, location)
    key = contract.primary_key
    change = contract.change_column
    accepted: list[DataFrame] = []
    pending: list[tuple[IngestionManifest, int, int, int, str | None]] = []
    for m in manifests:
        records = read_records(spark, m)
        rows = validate(records, contract, m).localCheckpoint(eager=True)
        total = rows.count()
        expected = m.reconciliation.landed_record_count if m.reconciliation else None
        if total != expected:
            raise BronzeMismatchError(
                f"batch {m.batch_id}: read {total} records, manifest recorded {expected}"
            )
        bad = rows.where(F.size("reasons") > 0)
        rejected = bad.count()
        quarantine_uri = None
        if rejected:
            quarantine_uri = f"{quarantine_root.rstrip('/')}/silver/{contract.dataset}/batch_id={m.batch_id}/"
            bad.select(
                F.lit(contract.name).alias("contract"),
                "_batch_id",
                "_source_file",
                "_source_position",
                F.col("_record").alias("raw_record"),
                "reasons",
            ).coalesce(1).write.mode("errorifexists").json(quarantine_uri)
        ratio = rejected / total if total else 0.0
        window = m.window.start_date.isoformat() if m.window else ""
        if ratio > contract.max_reject_ratio:
            logger.error("batch %s: %d/%d rejected; blocked", m.batch_id, rejected, total)
            run.batches.append(
                BatchResult(
                    m.batch_id,
                    window,
                    _purpose(m),
                    "blocked",
                    total,
                    total - rejected,
                    rejected,
                    ratio,
                    quarantine_uri,
                )
            )
            continue
        accepted.append(rows.where(F.size("reasons") == 0).drop("reasons", "_record"))
        pending.append((m, total, total - rejected, rejected, quarantine_uri))

    if not accepted:
        run.rows_after = spark.table(fq_table).count()
        return run

    incoming = accepted[0]
    for df in accepted[1:]:
        incoming = incoming.unionByName(df)
    ranked = incoming.withColumn(
        "_rank",
        F.row_number().over(
            Window.partitionBy(*key).orderBy(F.col(change).desc(), F.col("_batch_id").desc())
        ),
    ).localCheckpoint(eager=True)
    winners = ranked.where("_rank = 1").drop("_rank")
    superseded = ranked.where("_rank > 1").groupBy("_batch_id").count()

    # What each batch found against Silver before the MERGE (open question 30's measurement).
    target = spark.table(fq_table).select(
        *key,
        F.col(change).alias("_t_change"),
        F.col("_row_hash").alias("_t_hash"),
        F.col("_batch_id").alias("_t_batch"),
    )
    outcome = (
        winners.join(target, on=key, how="left")
        .withColumn(
            "_outcome",
            F.when(F.col("_t_change").isNull(), "new")
            .when(F.col(change) > F.col("_t_change"), "changed")
            .when(F.col(change) < F.col("_t_change"), "older")
            .when(F.col("_row_hash") == F.col("_t_hash"), "unchanged")
            .when(F.col("_batch_id") > F.col("_t_batch"), "changed")
            .otherwise("older"),
        )
        .groupBy("_batch_id", "_outcome")
        .count()
        .collect()
    )
    counts: dict[tuple[str, str], int] = {(r["_batch_id"], r["_outcome"]): r["count"] for r in outcome}
    superseded_by = {r["_batch_id"]: r["count"] for r in superseded.collect()}

    view = f"_merge_{contract.name}"
    winners.withColumn("_silver_updated_at", F.current_timestamp()).createOrReplaceTempView(view)
    cols = [c.name for c in contract.columns] + [
        "currency_code",
        "_batch_id",
        "_window_date",
        "_source_file",
        "_source_position",
        "_row_hash",
        "_silver_updated_at",
    ]
    on = " AND ".join(f"t.`{k}` = s.`{k}`" for k in key)
    newer = (
        f"s.`{change}` > t.`{change}` OR "
        f"(s.`{change}` = t.`{change}` AND s._row_hash <> t._row_hash AND s._batch_id > t._batch_id)"
    )
    set_clause = ", ".join(f"t.`{c}` = s.`{c}`" for c in cols)
    insert_cols = ", ".join(f"`{c}`" for c in cols)
    insert_vals = ", ".join(f"s.`{c}`" for c in cols)
    spark.sql(
        f"""
        MERGE INTO {fq_table} t
        USING {view} s
        ON {on}
        WHEN MATCHED AND ({newer}) THEN UPDATE SET {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
        """  # noqa: S608 - the table name is validated; columns come from the contract
    )

    # Validation before the run counts as applied: no incoming key may be newer than Silver now.
    behind = (
        winners.select(*key, F.col(change).alias("_in"))
        .join(spark.table(fq_table).select(*key, F.col(change).alias("_now")), on=key, how="left")
        .where(F.col("_now").isNull() | (F.col("_now") < F.col("_in")))
        .count()
    )
    if behind:
        raise RuntimeError(f"{fq_table}: {behind} incoming keys are not current in Silver after the MERGE")

    for m, total, valid, rejected, quarantine_uri in pending:
        b = m.batch_id
        run.batches.append(
            BatchResult(
                b,
                m.window.start_date.isoformat() if m.window else "",
                _purpose(m),
                "applied",
                total,
                valid,
                rejected,
                rejected / total if total else 0.0,
                quarantine_uri,
                new_keys=counts.get((b, "new"), 0),
                changed_keys=counts.get((b, "changed"), 0),
                unchanged_keys=counts.get((b, "unchanged"), 0),
                older_than_silver=counts.get((b, "older"), 0),
                superseded_in_run=superseded_by.get(b, 0),
            )
        )
    run.rows_after = spark.table(fq_table).count()
    snap = spark.sql(
        f"SELECT snapshot_id FROM {fq_table}.snapshots ORDER BY committed_at DESC LIMIT 1"  # noqa: S608
    ).collect()
    run.snapshot_id = snap[0].snapshot_id if snap else None
    return run
