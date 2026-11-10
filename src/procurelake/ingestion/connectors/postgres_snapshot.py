"""Consistent full snapshot of the operational PostgreSQL tables into Bronze.

This is the batch path for operational data until CDC takes over.

* **One consistent snapshot.** Every table is read inside a single ``REPEATABLE READ,
  READ ONLY`` transaction, so all tables reflect the same database state. Each table's
  ``count(*)`` is taken in that same snapshot, so reconciliation is exact. The snapshot ID
  (``pg_current_snapshot()``) and WAL position (``pg_current_wal_lsn()``) are recorded in each
  manifest. They mark the point a later CDC stream must continue from.
* **Source fidelity.** Rows are landed as PostgreSQL's own ``row_to_json`` text, one JSON
  object per line (JSON Lines). Numerics keep their exact digits (``125000.00``), timestamps keep
  their offset, and nothing is re-typed by Python.
* **Contract-checked.** Each table's live columns are compared with its source contract
  before it is read. A mismatch fails that table's batch and nothing is landed for it.

Layout: ``bronze/postgres/<table>/ingestion_date=<UTC date>/batch_id=<id>/part-NNNNN.jsonl``,
one manifest per table under ``pipeline_manifests/postgres/<table>/``. All tables in a run share
the same batch ID.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import IsolationLevel, sql

from procurelake.contracts.postgres import schema_differences
from procurelake.contracts.source import SourceContract
from procurelake.ingestion.batch import code_version, new_batch_id
from procurelake.ingestion.landing import LandingStore
from procurelake.ingestion.manifest import IngestionManifest, LandedFile, Reconciliation, write_manifest

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "postgres"
DEFAULT_ROWS_PER_FILE = 50_000
# Parents before children, matching the foreign keys (also the load order of the generator).
TABLE_ORDER = (
    "tenants",
    "agencies",
    "suppliers",
    "solicitations",
    "awards",
    "purchase_orders",
    "invoices",
    "contract_documents",
)


@dataclass(frozen=True)
class SnapshotPoint:
    snapshot: str
    wal_lsn: str
    database_time: datetime


def order_contracts(contracts: Iterable[SourceContract]) -> list[SourceContract]:
    by_name = {c.name: c for c in contracts if c.physical.system == "postgresql"}
    unknown = sorted(set(by_name) - set(TABLE_ORDER))
    if unknown:
        raise ValueError(f"no load order defined for contracted tables: {unknown}")
    return [by_name[name] for name in TABLE_ORDER if name in by_name]


class PostgresSnapshotConnector:
    def __init__(
        self,
        dsn: str,
        raw_store: LandingStore,
        manifest_store: LandingStore,
        contracts: Iterable[SourceContract],
        rows_per_file: int = DEFAULT_ROWS_PER_FILE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if rows_per_file < 1:
            raise ValueError("rows_per_file must be >= 1")
        self.dsn = dsn
        self.raw_store = raw_store
        self.manifest_store = manifest_store
        self.contracts = order_contracts(contracts)
        self.rows_per_file = rows_per_file
        self.clock = clock

    def snapshot(self) -> list[IngestionManifest]:
        started = self.clock().replace(microsecond=0)
        batch_id = new_batch_id(started)
        manifests: list[IngestionManifest] = []

        with psycopg.connect(self.dsn) as conn:
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            with conn.transaction():
                snapshot_text, lsn, db_now = conn.execute(
                    "SELECT pg_current_snapshot()::text, pg_current_wal_lsn()::text, now()"
                ).fetchone()
                point = SnapshotPoint(snapshot=snapshot_text, wal_lsn=lsn, database_time=db_now)
                for contract in self.contracts:
                    manifests.append(self._snapshot_table(conn, contract, batch_id, started, point))
        return manifests

    def _snapshot_table(
        self,
        conn: psycopg.Connection,
        contract: SourceContract,
        batch_id: str,
        started: datetime,
        point: SnapshotPoint,
    ) -> IngestionManifest:
        schema, table = contract.physical.schema_name, contract.physical.table
        pk = contract.primary_key
        if len(pk) != 1:
            raise ValueError(f"{table}: snapshot supports single-column primary keys only")
        qualified = sql.Identifier(schema, table)
        query = sql.SQL("SELECT {pk}::text, row_to_json(t)::text FROM {tbl} AS t ORDER BY {pk}").format(
            pk=sql.Identifier(pk[0]), tbl=qualified
        )
        prefix = (
            f"bronze/{SOURCE_SYSTEM}/{table}/ingestion_date={started.date().isoformat()}/batch_id={batch_id}"
        )

        files: list[LandedFile] = []
        landed = 0
        keys: set[str] = set()
        expected: int | None = None
        error: str | None = None

        differences = schema_differences(conn, contract)
        if differences:
            error = "schema does not match contract: " + "; ".join(differences)
        else:
            expected = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(qualified)).fetchone()[0]
            with conn.cursor(name=f"snapshot_{table}") as cursor:
                cursor.itersize = 5_000
                cursor.execute(query)
                part = 0
                while True:
                    rows = cursor.fetchmany(self.rows_per_file)
                    if not rows:
                        break
                    part += 1
                    body = "".join(f"{row_json}\n" for _, row_json in rows).encode("utf-8")
                    stored = self.raw_store.put_new(
                        f"{prefix}/part-{part:05d}.jsonl", body, "application/x-ndjson"
                    )
                    files.append(
                        LandedFile(
                            key=stored.key,
                            uri=stored.uri,
                            size_bytes=stored.size_bytes,
                            sha256=stored.sha256_hex,
                            record_count=len(rows),
                            page=part,
                        )
                    )
                    landed += len(rows)
                    keys.update(key for key, _ in rows)

        passed = error is None and expected == landed == len(keys)
        if error:
            detail = "table not read"
        elif landed != len(keys):
            detail = "duplicate primary keys in snapshot"
        elif landed != expected:
            detail = "landed rows do not match count(*) in the same snapshot"
        else:
            detail = "landed rows match count(*) in the same snapshot; primary keys unique"

        manifest = IngestionManifest(
            source_system=SOURCE_SYSTEM,
            dataset=table,
            schema_version=f"postgres.{schema}.{table}.v{contract.contract_version}",
            batch_id=batch_id,
            status="succeeded" if passed else "failed",
            ingestion_started_at=started,
            ingestion_completed_at=self.clock().replace(microsecond=0),
            request={
                "mode": "full_snapshot",
                "table": f"{schema}.{table}",
                "query": query.as_string(conn),
                "isolation": "repeatable read, read only (single transaction for all tables)",
                "pg_snapshot": point.snapshot,
                "wal_lsn": point.wal_lsn,
                "database_time": point.database_time.isoformat(),
                "rows_per_file": self.rows_per_file,
                "format": "jsonl (row_to_json)",
            },
            files=files,
            reconciliation=Reconciliation(
                expected_count_before=expected,
                expected_count_after=None,
                landed_record_count=landed,
                unique_record_count=len(keys),
                passed=passed,
                detail=detail,
            ),
            code_version=code_version(),
            error=error,
        )
        key = write_manifest(self.manifest_store, manifest)
        logger.info("%s batch %s %s: %s (%s)", table, batch_id, manifest.status, key, detail)
        return manifest
