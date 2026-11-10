"""Compare a PostgreSQL table's live catalog with its source contract."""

from __future__ import annotations

import psycopg

from procurelake.contracts.source import SourceContract


def _contract_type(data_type: str, char_len: int | None, precision: int | None, scale: int | None) -> str:
    if data_type == "character":
        return f"char({char_len})"
    if data_type == "numeric":
        return f"numeric({precision},{scale})"
    return {"timestamp with time zone": "timestamptz"}.get(data_type, data_type)


def live_columns(conn: psycopg.Connection, schema: str, table: str) -> list[tuple[str, str, bool]]:
    """(name, contract-style type, nullable) for each column, in ordinal order."""
    rows = conn.execute(
        """
        SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale,
               is_nullable = 'YES'
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return [(name, _contract_type(dt, cl, p, s), nullable) for name, dt, cl, p, s, nullable in rows]


def schema_differences(conn: psycopg.Connection, contract: SourceContract) -> list[str]:
    """Human-readable differences between the live table and the contract (empty if none)."""
    live = live_columns(conn, contract.physical.schema_name, contract.physical.table)
    if not live:
        return [f"table {contract.physical.schema_name}.{contract.physical.table} does not exist"]
    expected = [(c.name, c.type, c.nullable) for c in contract.columns]
    if live == expected:
        return []
    live_by_name = {name: (t, n) for name, t, n in live}
    expected_by_name = {name: (t, n) for name, t, n in expected}
    diffs = [f"missing column {name}" for name in expected_by_name if name not in live_by_name]
    diffs += [f"unexpected column {name}" for name in live_by_name if name not in expected_by_name]
    diffs += [
        f"column {name}: contract {expected_by_name[name]} != live {live_by_name[name]}"
        for name in expected_by_name
        if name in live_by_name and expected_by_name[name] != live_by_name[name]
    ]
    if not diffs:
        diffs.append("column order differs from contract")
    return diffs
