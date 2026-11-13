"""Helpers for the acceptance tests (plan §3): the e2e dataset's specs, PostgreSQL vs Silver
state, and ``DmsDouble``, a test-only stand-in for AWS DMS (see conftest.py).
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from procurelake.cdc.apply import active_rows
from procurelake.contracts.source import load_contracts
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.lakehouse.silver.spec import SilverTableSpec, load_specs

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = load_contracts(REPO / "contracts" / "source")
SPECS = load_specs(CONTRACTS, REPO / "configs" / "pipelines" / "silver")
CDC_ROOT = "bronze/postgres_cdc"
# Columns compared between PostgreSQL and Silver: every column except timestamps (PySpark
# converts collected timestamps to the process's local zone; ordering and freshness of
# timestamps are covered by the CDC apply tests).
COMPARABLE = ("string", "date", "decimal(18,2)")


def comparable_columns(spec: SilverTableSpec) -> list[str]:
    return [name for name, stype in spec.columns if stype in COMPARABLE]


def _plain(value):
    return str(value) if isinstance(value, uuid.UUID) else value


def source_state(dsn: str, spec: SilverTableSpec) -> dict[str, tuple]:
    """PostgreSQL's current rows for ``spec``: primary key -> comparable values."""
    cols = comparable_columns(spec)
    (pk,) = spec.primary_key
    query = sql.SQL("SELECT {cols} FROM procurement.{table}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols), table=sql.Identifier(spec.source_table)
    )
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(query).fetchall()
    return {str(r[pk]): tuple(_plain(r[c]) for c in cols) for r in rows}


def silver_state(spark, fq_table: str, spec: SilverTableSpec) -> dict[str, tuple]:
    """Silver's current-state rows (tombstones removed): primary key -> comparable values."""
    cols = comparable_columns(spec)
    (pk,) = spec.primary_key
    rows = active_rows(spark.table(fq_table)).select(*cols).collect()
    return {r[pk]: tuple(r[c] for c in cols) for r in rows}


def duplicate_keys(spark, fq_table: str, spec: SilverTableSpec) -> int:
    pk = ", ".join(spec.primary_key)  # contract identifiers and a test-owned table name
    query = f"SELECT count(*) FROM (SELECT {pk} FROM {fq_table} GROUP BY {pk} HAVING count(*) > 1)"
    return spark.sql(query).first()[0]


class DmsDouble:
    """Writes PostgreSQL rows as DMS-shaped Parquet under the Bronze CDC root (test double)."""

    def __init__(self, spark, raw: LocalLandingStore, root: Path, load_id: str):
        self.spark, self.raw, self.root, self.load_id = spark, raw, root, load_id

    def _schema(self, spec: SilverTableSpec) -> str:
        return ", ".join(["Op string", "_dms_commit_ts string", *(f"`{n}` {t}" for n, t in spec.columns)])

    @staticmethod
    def _ts(when: datetime) -> str:
        return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    def _row(self, spec: SilverTableSpec, op: str, when: datetime, after: Mapping | None, key: str) -> tuple:
        if op == "D":  # DMS deletes carry only the primary key
            values = [key if name in spec.primary_key else None for name, _ in spec.columns]
        else:
            values = [_plain(after[name]) for name, _ in spec.columns]
        return (op, self._ts(when), *values)

    def write(self, spec: SilverTableSpec, name: str, rows: Iterable[tuple]) -> str:
        key = f"{CDC_ROOT}/load_id={self.load_id}/procurement/{spec.source_table}/{name}"
        target = self.root / "raw" / key
        tmp = self.root / f"_dms_{uuid.uuid4().hex}"
        self.spark.createDataFrame(list(rows), self._schema(spec)).coalesce(1).write.parquet(str(tmp))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(next(tmp.glob("part-*.parquet"))), target)
        shutil.rmtree(tmp)
        return key

    def full_load(self, dsn: str, spec: SilverTableSpec, when: datetime) -> str:
        with psycopg.connect(dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                sql.SQL("SELECT * FROM procurement.{}").format(sql.Identifier(spec.source_table))
            ).fetchall()
        (pk,) = spec.primary_key
        return self.write(
            spec, "LOAD00000001.parquet", [self._row(spec, "I", when, r, str(r[pk])) for r in rows]
        )

    def changes(self, spec: SilverTableSpec, changes: Iterable, name: str | None = None) -> str | None:
        """One CDC file for the given workload changes of ``spec``'s table (None if there are none)."""
        mine = [c for c in changes if c.table == spec.source_table]
        if not mine:
            return None
        name = (
            name
            or max(c.committed_at for c in mine).astimezone(UTC).strftime("%Y%m%d-%H%M%S%f")[:-3] + ".parquet"
        )
        return self.write(spec, name, [self._row(spec, c.kind, c.committed_at, c.after, c.key) for c in mine])
