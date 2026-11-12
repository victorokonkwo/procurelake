"""verify.check_layers decides per key from Athena rows (run_query replaced by a test double)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from procurelake.cdc import verify

A, B, C = (f"00000000-0000-4000-8000-00000000000{i}" for i in range(1, 4))


@dataclass
class Change:
    table: str
    key: str
    kind: str
    after: dict | None


@dataclass
class Result:
    rows: list


def _fake(silver_rows, gold_rows):
    def run_query(_athena, sql, _wg, database):
        return Result(silver_rows if database == "silver" else gold_rows)

    return run_query


def test_matching_state_has_no_mismatches(monkeypatch):
    changes = [
        Change("invoices", A, "I", {"payment_status": "pending"}),
        Change("invoices", A, "U", {"payment_status": "paid"}),  # latest wins
        Change("invoices", B, "D", None),
    ]
    monkeypatch.setattr(
        verify, "run_query", _fake([(A, "paid", "false"), (B, "pending", "true")], [(A, "paid")])
    )
    assert verify.check_layers(None, "wg", "silver", "gold", changes) == ([], [])


def test_reports_each_layer_separately(monkeypatch):
    changes = [Change("invoices", A, "U", {"payment_status": "paid"}), Change("invoices", B, "D", None)]
    monkeypatch.setattr(
        verify,
        "run_query",
        _fake([(A, "pending", "false"), (B, "pending", "false")], [(A, "paid"), (B, "x")]),
    )
    silver_bad, gold_bad = verify.check_layers(None, "wg", "silver", "gold", changes)
    assert len(silver_bad) == 2 and "not a tombstone" in silver_bad[1]
    assert gold_bad == [f"fct_invoice {B}: deleted key still in Gold"]


def test_refuses_non_uuid_keys(monkeypatch):
    monkeypatch.setattr(verify, "run_query", _fake([], []))
    with pytest.raises(ValueError):
        verify.check_layers(
            None, "wg", "silver", "gold", [Change("invoices", "x' OR '1'='1", "U", {"payment_status": "p"})]
        )


def test_large_key_sets_are_split_under_athena_query_length_limit(monkeypatch):
    keys = [f"00000000-0000-4000-8000-{i:012d}" for i in range(4_500)]
    changes = [Change("invoices", k, "U", {"payment_status": "paid"}) for k in keys]
    sqls: list[str] = []

    def run_query(_athena, sql, _wg, database):
        sqls.append(sql)
        in_sql = [k for k in keys if k in sql]
        if database == "silver":
            return Result([(k, "paid", "false") for k in in_sql])
        return Result([(k, "paid") for k in in_sql])

    monkeypatch.setattr(verify, "run_query", run_query)
    assert verify.check_layers(None, "wg", "silver", "gold", changes) == ([], [])
    assert len(sqls) == 2 * 3  # 4,500 keys -> 3 chunks, Silver and Gold each
    assert max(len(s.encode()) for s in sqls) < 262_144
