"""Concurrent tenant benchmark logic with a fake Athena client per tenant (no AWS)."""

from __future__ import annotations

import pytest

from procurelake.benchmarks.concurrent_tenants import (
    MARTS,
    IsolationBreach,
    run_benchmark,
    tenant_sql,
)


class FakeAthena:
    """Answers every query with the given tenant rows."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows, self.started = rows, 0

    def start_query_execution(self, **_: object) -> dict:
        self.started += 1
        return {"QueryExecutionId": f"q{self.started}"}

    def get_query_execution(self, QueryExecutionId: str) -> dict:
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {
                    "EngineExecutionTimeInMillis": 500,
                    "TotalExecutionTimeInMillis": 700,
                    "QueryQueueTimeInMillis": 100,
                    "DataScannedInBytes": 10,
                },
                "ResultConfiguration": {"OutputLocation": "s3://r/x"},
            }
        }

    def get_paginator(self, _: str):
        rows = self.rows

        class P:
            def paginate(self, **_: object):
                header = {"Data": [{"VarCharValue": "tenant_id"}, {"VarCharValue": "row_count"}]}
                data = [{"Data": [{"VarCharValue": v} for v in r]} for r in rows]
                yield {
                    "ResultSet": {
                        "ResultSetMetadata": {"ColumnInfo": [{"Name": "tenant_id"}, {"Name": "row_count"}]},
                        "Rows": [header, *data],
                    }
                }

        return P()


WG = {"tenant_alder": "wg-a", "tenant_birch": "wg-b"}


def test_both_phases_run_every_query_for_each_tenant():
    clients = {
        "tenant_alder": FakeAthena([("tenant_alder", "5")]),
        "tenant_birch": FakeAthena([("tenant_birch", "7")]),
    }
    body = run_benchmark(clients, WG, "gold_db", rounds=2, sleep=lambda _s: None)
    assert clients["tenant_alder"].started == 2 * 2 * len(MARTS)  # two phases x two rounds
    assert len(body["runs"]["concurrent"]) == 2 * 2 * len(MARTS)
    assert body["summary"]["concurrent"]["tenant_birch"]["total_ms"]["median"] == 700


def test_a_row_from_the_other_tenant_fails_the_run():
    clients = {
        "tenant_alder": FakeAthena([("tenant_alder", "5"), ("tenant_birch", "1")]),
        "tenant_birch": FakeAthena([("tenant_birch", "7")]),
    }
    with pytest.raises(IsolationBreach, match="tenant_alder saw"):
        run_benchmark(clients, WG, "gold_db", rounds=1, sleep=lambda _s: None)


def test_only_known_marts_and_safe_databases_are_queried():
    assert "gold_db.mart_actual_spend" in tenant_sql("gold_db", "mart_actual_spend")
    with pytest.raises(ValueError):
        tenant_sql("gold; drop", "mart_actual_spend")
    with pytest.raises(ValueError):
        tenant_sql("gold_db", "fct_invoice")
