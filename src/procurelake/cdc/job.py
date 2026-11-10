"""Apply new Bronze CDC files for one Silver table, with a lease and a checkpoint (C3/C4).

Sequence per table (ADR 0028):

1. Take the table's DynamoDB lease (no overlapping writers).
2. List the table's DMS files in Bronze and drop those already recorded as applied.
3. For each load_id in order, apply its new files (``apply.apply_events``).
4. Only after the Iceberg write **and** its validation succeed, record the files and advance the
   checkpoint (a transaction that re-checks the lease).
5. Release the lease, also on failure.

A load_id whose full load DMS has not finished is deferred whole (``deferred_load_ids``).

A failure between steps 3 and 4 leaves the files unrecorded: the next run reads them again and
the idempotent apply makes the replay a no-op (C4). A quality-gate block stops the table without
recording anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

from procurelake.cdc.apply import CdcApplyResult, apply_events, is_full_load_file, load_id_of
from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.ingestion.batch import new_batch_id
from procurelake.ingestion.landing import LandingStore
from procurelake.lakehouse.silver.spec import SilverTableSpec


def table_files(store: LandingStore, cdc_root: str, schema: str, table: str) -> list[str]:
    """Bronze keys of every DMS Parquet file for ``schema.table``, across all load_ids."""
    marker = f"/{schema}/{table}/"
    return [
        k for k in store.list_keys(f"{cdc_root.rstrip('/')}/") if marker in f"/{k}" and k.endswith(".parquet")
    ]


def run_cdc_table(
    spark,
    spec: SilverTableSpec,
    store: LandingStore,
    cdc_root: str,
    checkpoints: CheckpointStore,
    fq_table: str,
    quarantine_root: str,
    owner: str,
    source_schema: str = "procurement",
    table_location: str | None = None,
    complete_full_loads: frozenset[str] = frozenset(),
    deferred_load_ids: frozenset[str] = frozenset(),
    lease_ttl_seconds: int = 1800,
) -> list[CdcApplyResult]:
    lease = checkpoints.acquire_lease(spec.silver_table, owner, lease_ttl_seconds)
    results: list[CdcApplyResult] = []
    try:
        applied = checkpoints.applied_files(spec.silver_table)
        pending = [
            k for k in table_files(store, cdc_root, source_schema, spec.source_table) if k not in applied
        ]
        by_load: dict[str, list[str]] = {}
        for key in pending:
            by_load.setdefault(load_id_of(f"/{key}"), []).append(key)
        all_files = table_files(store, cdc_root, source_schema, spec.source_table)
        for load_id in sorted(by_load):
            if load_id in deferred_load_ids:
                # Its full load is still running: applying part of it now would record the
                # load files as applied without the complete-load tombstoning, and that could
                # never be redone. Wait for a later run (and keep later load_ids waiting too).
                break
            keys = by_load[load_id]
            load_files = {k for k in all_files if load_id_of(f"/{k}") == load_id and is_full_load_file(k)}
            # Tombstone absent keys only when this batch carries the whole, completed full load.
            full_load_complete = (
                load_id in complete_full_loads and bool(load_files) and load_files <= set(keys)
            )
            result = apply_events(
                spark,
                spec,
                fq_table,
                [store.uri_for(k) for k in keys],
                new_batch_id(datetime.now(UTC)),
                quarantine_root,
                table_location,
                full_load_complete=full_load_complete,
            )
            results.append(result)
            if result.status != "applied":
                break  # blocked: nothing recorded, later load_ids wait
            checkpoints.record_applied(lease, keys, result.batch_id, result.last_sequence, result.events)
    finally:
        checkpoints.release_lease(lease)
    return results
