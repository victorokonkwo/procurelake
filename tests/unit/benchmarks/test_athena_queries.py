"""Benchmark runner: the query set loads and renders, runs are recorded, comparisons refuse
different answers and reused results (fake Athena client; no AWS)."""

from __future__ import annotations

from pathlib import Path

import pytest

from procurelake.benchmarks.athena_queries import (
    compare,
    fingerprint,
    load_queries,
    markdown,
    render,
    run_benchmark,
)

REPO = Path(__file__).resolve().parents[3]
QUERIES = REPO / "configs" / "benchmarks" / "athena_queries.yaml"


class FakeAthena:
    """Returns the same rows for every query; statistics scripted per call."""

    def __init__(self, rows=(("CAD", "10"),), scanned=1000, reused=False, vary=False):
        self.rows, self.scanned, self.reused, self.vary = rows, scanned, reused, vary
        self.calls: list[tuple[str, str]] = []

    def start_query_execution(self, QueryString, WorkGroup, QueryExecutionContext):
        self.calls.append((QueryString, QueryExecutionContext["Database"]))
        return {"QueryExecutionId": f"q{len(self.calls)}"}

    def get_query_execution(self, QueryExecutionId):
        n = int(QueryExecutionId[1:])
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {
                    "DataScannedInBytes": self.scanned,
                    "EngineExecutionTimeInMillis": 100 + n,
                    "TotalExecutionTimeInMillis": 150 + n,
                    "QueryPlanningTimeInMillis": 20,
                    "QueryQueueTimeInMillis": 5,
                    "ResultReuseInformation": {"ReusedPreviousResult": self.reused},
                },
                "ResultConfiguration": {"OutputLocation": "s3://x/"},
            }
        }

    def get_paginator(self, _name):
        fake = self

        class Paginator:
            def paginate(self, QueryExecutionId):
                rows = list(fake.rows)
                if fake.vary:
                    rows = [(*rows[0][:-1], QueryExecutionId)]
                header = {"Data": [{"VarCharValue": "a"}, {"VarCharValue": "b"}]}
                data = [{"Data": [{"VarCharValue": v} for v in r]} for r in rows]
                yield {
                    "ResultSet": {
                        "ResultSetMetadata": {"ColumnInfo": [{"Name": "a"}, {"Name": "b"}]},
                        "Rows": [header, *data],
                    }
                }

        return Paginator()


def test_repository_query_set_loads_and_renders():
    queries = load_queries(QUERIES)
    assert len(queries) >= 6
    assert {q.layer for q in queries} == {"silver", "gold"}
    assert any(q.prune_on for q in queries) and any(not q.prune_on for q in queries)
    for q in queries:
        sql = render(q, "procurelake_dev_bench_silver", "procurelake_dev_bench_gold")
        assert "{" not in sql and ("procurelake_dev_bench_" in sql)
    with pytest.raises(ValueError, match="unsafe"):
        render(queries[0], "x; drop", "g")


def test_run_records_every_repeat_and_routes_by_layer():
    fake = FakeAthena()
    queries = load_queries(QUERIES)
    body = run_benchmark(fake, "wg", queries, "s_db", "g_db", repeats=3, label="a", sleep=lambda _s: None)
    assert len(fake.calls) == 3 * len(queries)
    for result in body["queries"]:
        assert len(result["runs"]) == 3
        assert result["summary"]["engine_ms"]["min"] <= result["summary"]["engine_ms"]["median"]
        assert result["summary"]["scanned_bytes"]["median"] == 1000
    assert {db for _, db in fake.calls} == {"s_db", "g_db"}
    assert all(r["answer"]["columns"] == ["a", "b"] for r in body["queries"])


def test_replay_check_set_loads_and_renders():
    queries = load_queries(QUERIES.parent / "replay_checks.yaml")
    assert {q.name for q in queries} >= {"silver_awards_keys", "gold_facts_keys"}
    for q in queries:
        sql = render(q, "procurelake_dev_bench_silver", "procurelake_dev_bench_gold")
        assert "{" not in sql and ("duplicate_rows" in sql or "total" in sql)


def test_reused_results_and_changing_answers_are_not_measurements():
    queries = load_queries(QUERIES)[:1]
    with pytest.raises(RuntimeError, match="reused"):
        run_benchmark(FakeAthena(reused=True), "wg", queries, "s", "g", 2, "a", sleep=lambda _s: None)
    with pytest.raises(RuntimeError, match="differed"):
        run_benchmark(FakeAthena(vary=True), "wg", queries, "s", "g", 2, "a", sleep=lambda _s: None)


def test_compare_refuses_a_different_answer():
    queries = load_queries(QUERIES)[:2]
    before = run_benchmark(
        FakeAthena(scanned=1000), "wg", queries, "s", "g", 1, "flat", sleep=lambda _s: None
    )
    after = run_benchmark(
        FakeAthena(scanned=100), "wg", queries, "s", "g", 1, "monthly", sleep=lambda _s: None
    )
    rows = compare(before, after)
    assert [(r["scanned_before"], r["scanned_after"]) for r in rows] == [(1000, 100), (1000, 100)]
    assert "| Scanned flat | Scanned monthly |" in markdown(rows, "flat", "monthly")
    changed = run_benchmark(
        FakeAthena(rows=(("USD", "9"),)), "wg", queries, "s", "g", 1, "x", sleep=lambda _s: None
    )
    with pytest.raises(ValueError, match="results differ"):
        compare(before, changed)


def test_fingerprint_depends_on_order_and_nulls():
    assert fingerprint(["a"], [("1",), ("2",)]) != fingerprint(["a"], [("2",), ("1",)])
    assert fingerprint(["a"], [(None,)]) != fingerprint(["a"], [("",)])
