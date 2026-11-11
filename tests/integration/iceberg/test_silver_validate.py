"""Validation rules on hand-written Bronze lines (local Spark)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from procurelake.contracts.source import load_contracts
from procurelake.lakehouse.silver.spec import load_specs
from procurelake.lakehouse.silver.transform import validate

pytestmark = pytest.mark.spark

REPO = Path(__file__).resolve().parents[3]
SPECS = load_specs(load_contracts(REPO / "contracts" / "source"), REPO / "configs" / "pipelines" / "silver")

GOOD = {
    "award_id": "00000000-0000-4000-8000-000000000001",
    "tenant_id": "tenant_alder",
    "solicitation_id": "00000000-0000-4000-8000-0000000000a1",
    "supplier_id": "00000000-0000-4000-8000-0000000000b1",
    "award_amount": 125000.00,
    "currency_code": "CAD",
    "awarded_at": "2026-01-15T12:00:00+00:00",
    "status": "active",
    "created_at": "2026-01-15T12:00:00+00:00",
    "updated_at": "2026-01-15T12:00:00+00:00",
}


def _line(**overrides) -> str:
    row = {**GOOD, **overrides}
    return json.dumps({k: v for k, v in row.items() if v is not ...})


def _run(spark, lines: list[str]):
    df = spark.createDataFrame(
        [(line, "file:///bronze/part-00001.jsonl") for line in lines], "_raw_line string, _source_file string"
    )
    valid, rejected = validate(df, SPECS["awards"], "20261110T000000Z-test0001")
    return valid.collect(), {r.raw_line: sorted(r.reasons) for r in rejected.collect()}


def test_valid_row_is_typed_exactly(spark):
    valid, rejected = _run(
        spark,
        [
            '{"award_id":"00000000-0000-4000-8000-000000000001","tenant_id":"tenant_alder",'
            '"solicitation_id":"00000000-0000-4000-8000-0000000000a1",'
            '"supplier_id":"00000000-0000-4000-8000-0000000000b1","award_amount":125000.10,'
            '"currency_code":"CAD","awarded_at":"2026-01-15T12:00:00+00:00","status":"active",'
            '"created_at":"2026-01-15T07:00:00-05:00","updated_at":"2026-01-15T12:00:00+00:00"}'
        ],
    )
    assert rejected == {}
    [row] = valid
    assert row.award_amount == Decimal("125000.10")
    # PySpark's collect() renders timestamps as naive datetimes in the *machine's* local zone,
    # so check the stored instant inside Spark (session zone UTC): -05:00 must be honoured.
    created_utc = (
        spark.createDataFrame([(row.created_at,)], "ts timestamp")
        .selectExpr("date_format(ts, \"yyyy-MM-dd'T'HH:mm:ssXXX\") AS s")
        .first()
        .s
    )
    assert created_utc in {"2026-01-15T12:00:00Z", "2026-01-15T12:00:00+00:00"}
    assert row._batch_id == "20261110T000000Z-test0001"
    assert len(row._row_hash) == 64 and len(row._source_line_hash) == 64


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("not json at all", ["unparseable_json"]),
        (_line(award_amount="lots"), ["type_mismatch:award_amount"]),
        (_line(awarded_at="yesterday"), ["type_mismatch:awarded_at"]),
        # Presence comes from one map<string,string> parse (ADR 0034): non-string JSON values
        # must still count as supplied, so a wrong type is a type_mismatch, never not_null.
        (_line(award_amount={"value": 1}), ["type_mismatch:award_amount"]),
        (_line(award_amount=[1, 2]), ["type_mismatch:award_amount"]),
        (_line(supplier_id=...), ["not_null:supplier_id"]),
        (_line(supplier_id=None), ["not_null:supplier_id"]),
        (_line(status="suspended"), ["not_allowed:status"]),
        (_line(award_amount=0), ["rule:award_amount_positive"]),
        (_line(currency_code="cad"), ["rule:currency_code_format"]),
        (_line(currency_code="CADX"), ["too_long:currency_code"]),  # structural: rules not evaluated
    ],
)
def test_invalid_rows_are_rejected_with_reasons(spark, line, expected):
    valid, rejected = _run(spark, [line])
    assert valid == []
    assert rejected == {line: sorted(expected)}


def test_duplicate_keys_keep_latest_change(spark):
    older = _line(award_amount=100.0, updated_at="2026-01-15T12:00:00+00:00")
    newer = _line(award_amount=200.0, updated_at="2026-02-01T12:00:00+00:00")
    valid, rejected = _run(spark, [older, newer])
    assert [r.award_amount for r in valid] == [Decimal("200.00")]
    assert rejected == {older: ["duplicate_key_superseded"]}
