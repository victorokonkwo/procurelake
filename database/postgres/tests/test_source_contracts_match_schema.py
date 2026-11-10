"""Every PostgreSQL source contract must match the migrated catalog exactly: columns, types,
nullability, primary key, foreign keys, and the allowed values of CHECK-constrained columns."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from procurelake.contracts.source import SourceContract, load_contracts

pytestmark = pytest.mark.postgres

CONTRACTS = load_contracts(Path(__file__).resolve().parents[3] / "contracts" / "source")


def _pg_type(data_type: str, char_len: int | None, precision: int | None, scale: int | None) -> str:
    if data_type == "character":
        return f"char({char_len})"
    if data_type == "numeric":
        return f"numeric({precision},{scale})"
    return {"timestamp with time zone": "timestamptz"}.get(data_type, data_type)


def _columns(conn, schema: str, table: str) -> list[tuple[str, str, bool]]:
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
    return [(name, _pg_type(dt, cl, p, s), nullable) for name, dt, cl, p, s, nullable in rows]


def _constraints(
    conn, schema: str, table: str, contype: str
) -> list[tuple[list[str], str | None, list[str]]]:
    """(columns, referenced table, referenced columns) for constraints of ``contype`` (p or f)."""
    rows = conn.execute(
        """
        SELECT
          ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.i),
          rt.relname,
          ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(n, i)
                JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.i)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace ns ON ns.oid = t.relnamespace
        LEFT JOIN pg_class rt ON rt.oid = c.confrelid
        WHERE ns.nspname = %s AND t.relname = %s AND c.contype = %s
        """,
        (schema, table, contype),
    ).fetchall()
    return [(list(cols), ref, list(refcols)) for cols, ref, refcols in rows]


def _check_allowed_values(conn, schema: str, table: str, column: str) -> set[str] | None:
    """Literal values from the CHECK constraint of the form `column IN (...)`, if one exists."""
    rows = conn.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace ns ON ns.oid = t.relnamespace
        WHERE ns.nspname = %s AND t.relname = %s AND c.contype = 'c'
        """,
        (schema, table),
    ).fetchall()
    for (definition,) in rows:
        # PostgreSQL renders IN lists as: ((col = ANY (ARRAY['a'::text, 'b'::text])))
        if re.match(rf"^CHECK \(\({re.escape(column)} = ANY \(ARRAY\[", definition):
            return set(re.findall(r"'([^']*)'::text", definition))
    return None


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_columns_types_and_nullability_match(conn, contract: SourceContract):
    actual = _columns(conn, contract.physical.schema_name, contract.physical.table)
    expected = [(c.name, c.type, c.nullable) for c in contract.columns]
    assert actual == expected


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_primary_and_foreign_keys_match(conn, contract: SourceContract):
    schema, table = contract.physical.schema_name, contract.physical.table
    [(pk_columns, _, _)] = _constraints(conn, schema, table, "p")
    assert pk_columns == contract.primary_key

    actual_fks = sorted((cols, ref, refcols) for cols, ref, refcols in _constraints(conn, schema, table, "f"))
    expected_fks = sorted(
        (fk.columns, fk.references_table, fk.references_columns) for fk in contract.foreign_keys
    )
    assert actual_fks == expected_fks


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_allowed_values_match_check_constraints(conn, contract: SourceContract):
    schema, table = contract.physical.schema_name, contract.physical.table
    for column in contract.columns:
        actual = _check_allowed_values(conn, schema, table, column.name)
        expected = set(column.allowed_values) if column.allowed_values else None
        assert actual == expected, column.name
