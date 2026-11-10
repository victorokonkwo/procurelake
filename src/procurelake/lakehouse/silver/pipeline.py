"""Bronze snapshot batch -> Silver Iceberg table, one table at a time.

Steps per table:
  1. Read exactly the files the manifest lists. Fail if the line count differs from the
     manifest, which would mean Bronze changed or the manifest is wrong.
  2. Validate and deduplicate (``transform.validate``).
  3. Write rejected rows, with their reasons, to the quarantine location
     (``<quarantine_root>/silver/<table>/batch_id=<id>/``).
  4. Quality gate: if rejected/input exceeds the table's ``max_reject_ratio``, stop. Nothing
     is written to Silver.
  5. Apply to the Iceberg table (``writer.apply_snapshot``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from procurelake.ingestion.manifest import IngestionManifest
from procurelake.lakehouse.silver.spec import SilverTableSpec
from procurelake.lakehouse.silver.transform import read_bronze_lines, validate
from procurelake.lakehouse.silver.writer import ApplyResult, apply_snapshot, ensure_table

logger = logging.getLogger(__name__)


class BronzeMismatchError(RuntimeError):
    """Raised when Bronze files do not contain the rows their manifest recorded."""


@dataclass(frozen=True)
class SilverTableRun:
    table: str
    batch_id: str
    status: Literal["applied", "skipped", "blocked"]
    input_rows: int
    valid_rows: int
    rejected_rows: int
    reject_ratio: float
    quarantine_uri: str | None
    apply: ApplyResult | None


def select_snapshot_batch(manifests: Iterable[IngestionManifest], tables: Iterable[str]) -> str:
    """Latest batch ID with a succeeded snapshot manifest for every one of ``tables``."""
    required = set(tables)
    by_batch: dict[str, set[str]] = {}
    for m in manifests:
        if m.status == "succeeded" and m.source_system == "postgres" and m.window is None:
            by_batch.setdefault(m.batch_id, set()).add(m.dataset)
    complete = sorted(b for b, done in by_batch.items() if required <= done)
    if not complete:
        raise LookupError(f"no snapshot batch has succeeded manifests for all of {sorted(required)}")
    return complete[-1]


def run_table(
    spark: SparkSession,
    spec: SilverTableSpec,
    manifest: IngestionManifest,
    fq_table: str,
    quarantine_root: str,
    table_location: str | None = None,
) -> SilverTableRun:
    if manifest.status != "succeeded" or manifest.dataset != spec.source_table:
        raise ValueError(
            f"manifest {manifest.batch_id}/{manifest.dataset} is not a succeeded {spec.source_table} batch"
        )

    lines = read_bronze_lines(spark, [f.uri for f in manifest.files])
    input_rows = lines.count()
    expected = manifest.reconciliation.landed_record_count if manifest.reconciliation else None
    if input_rows != expected:
        raise BronzeMismatchError(
            f"{spec.source_table}: read {input_rows} lines, manifest recorded {expected}"
        )

    valid, rejected = validate(lines, spec, manifest.batch_id)
    valid = valid.cache()
    rejected = rejected.cache()
    valid_rows, rejected_rows = valid.count(), rejected.count()
    ratio = rejected_rows / input_rows if input_rows else 0.0

    quarantine_uri = None
    if rejected_rows:
        quarantine_uri = (
            f"{quarantine_root.rstrip('/')}/silver/{spec.source_table}/batch_id={manifest.batch_id}/"
        )
        rejected.coalesce(1).write.mode("errorifexists").json(quarantine_uri)

    if ratio > spec.config.max_reject_ratio:
        logger.error(
            "%s: %d/%d rows rejected (%.2f%% > %.2f%%); Silver not updated",
            spec.source_table,
            rejected_rows,
            input_rows,
            100 * ratio,
            100 * spec.config.max_reject_ratio,
        )
        return SilverTableRun(
            spec.source_table,
            manifest.batch_id,
            "blocked",
            input_rows,
            valid_rows,
            rejected_rows,
            ratio,
            quarantine_uri,
            None,
        )

    ensure_table(spark, spec, fq_table, table_location)
    key_types = dict(spec.columns)
    rejected_keys = rejected.select(
        *[F.col(f"key_{k}").cast(key_types[k]).alias(k) for k in spec.primary_key]
    ).where(" AND ".join(f"`{k}` IS NOT NULL" for k in spec.primary_key))
    result = apply_snapshot(spark, spec, fq_table, manifest.batch_id, valid, rejected_keys)
    valid.unpersist()
    rejected.unpersist()
    return SilverTableRun(
        spec.source_table,
        manifest.batch_id,
        "skipped" if result.skipped else "applied",
        input_rows,
        valid_rows,
        rejected_rows,
        ratio,
        quarantine_uri,
        result,
    )
