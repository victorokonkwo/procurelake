"""Data-quality checks: summary reconciliation and Silver integrity SQL (no AWS)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from procurelake.ingestion.landing import LocalLandingStore
from procurelake.ingestion.manifest import IngestionManifest, Reconciliation
from procurelake.lakehouse.silver.resources import load_silver_specs
from procurelake.quality.checks import (
    integrity_checks,
    integrity_sql,
    latest_silver_summary,
    report,
    summary_checks,
)


def _manifest(dataset: str, batch: str, count: int) -> IngestionManifest:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    return IngestionManifest(
        source_system="postgres",
        dataset=dataset,
        schema_version="v1",
        batch_id=batch,
        status="succeeded",
        ingestion_started_at=now,
        ingestion_completed_at=now,
        code_version="t",
        reconciliation=Reconciliation(
            expected_count_before=count,
            expected_count_after=count,
            landed_record_count=count,
            unique_record_count=count,
            passed=True,
            detail="ok",
        ),
    )


def _table(name: str, input_rows: int, valid: int, rejected: int, status="applied", uri=None) -> dict:
    return {
        "table": name,
        "batch_id": "b1",
        "input_rows": input_rows,
        "valid_rows": valid,
        "rejected_rows": rejected,
        "status": status,
        "quarantine_uri": uri,
        "reject_ratio": 0.0,
    }


def test_a_consistent_summary_passes_every_check():
    summary = {"source_system": "postgres", "tables": [_table("awards", 10, 9, 1, uri="s3://q/awards/")]}
    checks = summary_checks(
        summary, {("awards", "b1"): _manifest("awards", "b1", 10)}, lambda _u: 1, "postgres"
    )
    assert [c.name for c in checks] == [
        "manifest_count_equals_silver_input",
        "input_equals_valid_plus_rejected",
        "quarantine_holds_every_rejected_row",
        "table_not_blocked",
    ]
    assert all(c.passed for c in checks)


def test_each_inconsistency_is_reported():
    summary = {
        "source_system": "postgres",
        "tables": [
            _table("awards", 10, 9, 1, uri="s3://q/awards/"),  # quarantine has 0 lines
            _table("invoices", 5, 4, 0),  # 4 + 0 != 5; manifest says 6
            _table("suppliers", 3, 1, 2, status="blocked", uri="s3://q/suppliers/"),
        ],
    }
    manifests = {
        ("awards", "b1"): _manifest("awards", "b1", 10),
        ("invoices", "b1"): _manifest("invoices", "b1", 6),
        ("suppliers", "b1"): _manifest("suppliers", "b1", 3),
    }
    lines = {"s3://q/awards/": 0, "s3://q/suppliers/": 2}
    failed = {
        (c.name, c.subject.split("/")[1].split()[0])
        for c in summary_checks(summary, manifests, lines.get, "postgres")
        if not c.passed
    }
    assert failed == {
        ("quarantine_holds_every_rejected_row", "awards"),
        ("manifest_count_equals_silver_input", "invoices"),
        ("input_equals_valid_plus_rejected", "invoices"),
        ("table_not_blocked", "suppliers"),
    }


def test_a_missing_manifest_fails_reconciliation():
    summary = {"source_system": "postgres", "tables": [_table("awards", 10, 10, 0)]}
    (first, *_) = summary_checks(summary, {}, lambda _u: 0, "postgres")
    assert not first.passed and "manifest None" in first.detail


def test_latest_summary_is_chosen_by_completion_time(tmp_path):
    store = LocalLandingStore(tmp_path)
    for run, done in (("r1", "2026-11-10T00:00:00+00:00"), ("r2", "2026-11-12T00:00:00+00:00")):
        body = json.dumps({"completed_at": done, "run_id": run}).encode()
        store.put_new(
            f"pipeline_runs/silver/postgres/batch_id=b/run_id={run}/summary.json", body, "application/json"
        )
    key, body = latest_silver_summary(store, "postgres")
    assert body["run_id"] == "r2" and key.endswith("run_id=r2/summary.json")
    assert latest_silver_summary(store, "other") is None


def test_integrity_sql_uses_the_contract_primary_key():
    specs = load_silver_specs()
    sql = integrity_sql("silver_db", specs["awards"])
    assert "group by award_id having count(*) > 1" in sql
    assert "count_if(tenant_id is null)" in sql
    assert "from silver_db.silver_awards" in sql


class FakeAthena:
    def __init__(self, row):
        self.row = row

    def start_query_execution(self, **_):
        return {"QueryExecutionId": "q"}

    def get_query_execution(self, QueryExecutionId):
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {},
                "ResultConfiguration": {"OutputLocation": "s3://r/q"},
            }
        }

    def get_paginator(self, _):
        row = self.row

        class P:
            def paginate(self, **_):
                cols = ["total_rows", "duplicate_keys", "null_tenant_rows"]
                yield {
                    "ResultSet": {
                        "ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in cols]},
                        "Rows": [
                            {"Data": [{"VarCharValue": c} for c in cols]},
                            {"Data": [{"VarCharValue": str(v)} for v in row]},
                        ],
                    }
                }

        return P()


def test_integrity_checks_flag_duplicates_and_null_tenants():
    spec = load_silver_specs()["awards"]
    ok = integrity_checks(FakeAthena((100, 0, 0)), "wg", "silver_db", [spec], sleep=lambda _s: None)
    assert all(c.passed for c in ok) and len(ok) == 2
    bad = integrity_checks(FakeAthena((100, 2, 3)), "wg", "silver_db", [spec], sleep=lambda _s: None)
    assert [c.passed for c in bad] == [False, False]
    body = report(bad, run_id="r")
    assert body["passed"] is False and body["checks_failed"] == 2
