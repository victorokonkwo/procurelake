"""Iceberg table maintenance through Athena: retention properties, OPTIMIZE, VACUUM (ADR 0033).

For each table, in order:

1. ``ALTER TABLE ... SET TBLPROPERTIES`` with the layer's retention
   (``vacuum_max_snapshot_age_seconds``, ``vacuum_min_snapshots_to_keep``). Idempotent, and it
   re-applies retention to a table dbt or Glue recreated.
2. ``OPTIMIZE ... REWRITE DATA USING BIN_PACK``: rewrites small data files, applying and
   dropping their delete files. On a table that is already compact it changes nothing.
3. ``VACUUM``: expires snapshots older than the retention, always keeping the minimum number,
   and removes files no kept snapshot references (orphans older than the same age).

Health is measured before and after from the current snapshot's summary: snapshot count, data
and delete files, records, position deletes and bytes. Athena's ``$files`` lists data files
only, so delete files are read from the summary.

Writers are never raced:

* Silver tables written by the CDC apply job are maintained only while holding that table's
  DynamoDB lease (the same one ``procurelake.cdc`` takes); a held lease means the table is
  skipped this run.
* A statement that loses an Iceberg commit race (``ICEBERG_COMMIT_ERROR``) is retried.

Tables are independent: every table is attempted, then the run fails if any table failed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from procurelake.cdc.checkpoint import CheckpointStore, LeaseHeldError
from procurelake.common.athena import AthenaQueryError, run_query

logger = logging.getLogger(__name__)

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
COMMIT_CONFLICT = "ICEBERG_COMMIT_ERROR"
SUMMARY_KEYS = (
    "total-data-files",
    "total-delete-files",
    "total-records",
    "total-position-deletes",
    "total-equality-deletes",
    "total-files-size",
)


@dataclass(frozen=True)
class Retention:
    max_snapshot_age_seconds: int
    min_snapshots_to_keep: int

    def __post_init__(self) -> None:
        if self.max_snapshot_age_seconds < 3600 or self.min_snapshots_to_keep < 1:
            raise ValueError("retention must keep at least one hour and one snapshot")


@dataclass(frozen=True)
class TableTarget:
    database: str
    table: str
    retention: Retention
    lease: bool  # take the CDC writer lease (Silver tables the CDC apply job writes)

    def __post_init__(self) -> None:
        for name in (self.database, self.table):
            if not IDENTIFIER.fullmatch(name):
                raise ValueError(f"unsafe identifier: {name!r}")

    @property
    def fq(self) -> str:
        return f"{self.database}.{self.table}"


@dataclass(frozen=True)
class TableHealth:
    snapshots: int
    data_files: int
    delete_files: int
    records: int
    position_deletes: int
    equality_deletes: int
    bytes: int

    @property
    def live_records(self) -> int:
        """Rows a reader returns (exact when there are no equality deletes)."""
        return self.records - self.position_deletes - self.equality_deletes


@dataclass
class TableResult:
    target: TableTarget
    status: str  # maintained | skipped_lease_held | failed
    before: TableHealth | None = None
    after: TableHealth | None = None
    statements: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Athena:
    client: object
    workgroup: str
    sleep: Callable[[float], None] = time.sleep

    def run(self, sql: str, database: str, attempts: int = 3):
        for attempt in range(1, attempts + 1):
            try:
                return run_query(self.client, sql, self.workgroup, database, sleep=self.sleep)
            except AthenaQueryError as exc:
                if COMMIT_CONFLICT not in str(exc) or attempt == attempts:
                    raise
                logger.warning("commit conflict on attempt %d, retrying: %s", attempt, sql.split("\n")[0])
                self.sleep(5 * attempt)
        raise AssertionError("unreachable")


def health(athena: Athena, target: TableTarget) -> TableHealth:
    snapshots = f'"{target.database}"."{target.table}$snapshots"'
    picks = ", ".join(f"element_at(summary, '{k}')" for k in SUMMARY_KEYS)
    sql = (
        f"SELECT (SELECT count(*) FROM {snapshots}), {picks} FROM {snapshots} "  # noqa: S608 - identifiers checked
        "ORDER BY committed_at DESC LIMIT 1"
    )
    rows = athena.run(sql, target.database).rows
    if not rows:  # a table with no snapshot yet
        return TableHealth(0, 0, 0, 0, 0, 0, 0)
    count, *values = rows[0]
    data_files, delete_files, records, pos, eq, size = (int(v or 0) for v in values)
    return TableHealth(int(count), data_files, delete_files, records, pos, eq, size)


def statements(target: TableTarget) -> list[str]:
    r = target.retention
    return [
        f"ALTER TABLE {target.fq} SET TBLPROPERTIES ("
        f"'vacuum_max_snapshot_age_seconds'='{int(r.max_snapshot_age_seconds)}', "
        f"'vacuum_min_snapshots_to_keep'='{int(r.min_snapshots_to_keep)}')",
        f"OPTIMIZE {target.fq} REWRITE DATA USING BIN_PACK",
        f"VACUUM {target.fq}",
    ]


def maintain_table(athena: Athena, target: TableTarget) -> TableResult:
    result = TableResult(target, "failed")
    result.before = health(athena, target)
    for sql in statements(target):
        q = athena.run(sql, target.database)
        result.statements.append(
            {
                "sql": sql.split(" ", 1)[0],
                "query_id": q.query_execution_id,
                "scanned_bytes": q.data_scanned_bytes,
                "engine_ms": q.engine_execution_ms,
            }
        )
    result.after = health(athena, target)
    if result.after.live_records != result.before.live_records:
        raise RuntimeError(
            f"{target.fq}: live records changed during maintenance "
            f"({result.before.live_records} -> {result.after.live_records}); a writer may have run"
        )
    result.status = "maintained"
    return result


def run(
    athena: Athena,
    targets: list[TableTarget],
    checkpoints: CheckpointStore | None,
    owner: str,
    lease_ttl_seconds: int = 1800,
) -> list[TableResult]:
    results: list[TableResult] = []
    for target in targets:
        lease = None
        try:
            if target.lease:
                if checkpoints is None:
                    raise RuntimeError(f"{target.fq} needs the CDC lease but no checkpoint table was given")
                try:
                    lease = checkpoints.acquire_lease(target.table, owner, lease_ttl_seconds)
                except LeaseHeldError:
                    logger.info("%s: CDC writer holds the lease; skipped this run", target.fq)
                    results.append(TableResult(target, "skipped_lease_held"))
                    continue
            results.append(maintain_table(athena, target))
        except Exception as exc:  # attempt every table, then report
            logger.exception("%s: maintenance failed", target.fq)
            results.append(TableResult(target, "failed", error=str(exc)))
        finally:
            if lease is not None:
                checkpoints.release_lease(lease)
    return results
