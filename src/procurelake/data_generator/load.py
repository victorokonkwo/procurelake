"""Bulk-load a generated dataset into the operational PostgreSQL schema with COPY."""

from __future__ import annotations

import psycopg
from psycopg import sql

from procurelake.data_generator.model import Dataset, column_names

SCHEMA = "procurement"

# Every table in the schema, including those the generator does not populate, so that
# --reset leaves no stale rows behind.
ALL_TABLES = (
    "tenants",
    "agencies",
    "suppliers",
    "solicitations",
    "awards",
    "purchase_orders",
    "invoices",
    "contract_documents",
)


class TargetNotEmptyError(RuntimeError):
    """Raised when loading into tables that already contain rows without ``reset=True``."""


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts = {}
    for table in ALL_TABLES:
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(SCHEMA, table))
        counts[table] = conn.execute(query).fetchone()[0]
    return counts


def load_dataset(conn: psycopg.Connection, dataset: Dataset, *, reset: bool = False) -> dict[str, int]:
    """Load ``dataset`` in one transaction and return the resulting row counts.

    Refuses to load into non-empty tables unless ``reset`` is true. With ``reset``, every table
    in the schema is truncated first. Either the whole dataset loads or nothing changes.
    """
    with conn.transaction():
        if reset:
            conn.execute(
                sql.SQL("TRUNCATE {}").format(
                    sql.SQL(", ").join(sql.Identifier(SCHEMA, t) for t in ALL_TABLES)
                )
            )
        else:
            non_empty = {t: n for t, n in table_counts(conn).items() if n}
            if non_empty:
                raise TargetNotEmptyError(
                    f"Target tables are not empty: {non_empty}. Use reset to replace their contents."
                )

        for table, row_type, rows in dataset.tables():
            columns = column_names(row_type)
            copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(SCHEMA, table),
                sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            )
            with conn.cursor().copy(copy_sql) as copy:
                for row in rows:
                    copy.write_row([getattr(row, c) for c in columns])

        counts = table_counts(conn)
        expected = dataset.counts()
        mismatched = {t: (expected[t], counts[t]) for t in expected if expected[t] != counts[t]}
        if mismatched:
            raise RuntimeError(f"Row count mismatch after load (expected, actual): {mismatched}")
    return counts
