"""Acceptance test 12: performance. "Compare initial and optimized queries → actual
measurements are reported."

Live, on the isolated bench namespace (ADR 0034: the 1M-row synthetic load). The optimization
the platform uses for analytical questions is the governed Gold marts: aggregates maintained by
dbt (incremental merge, compacted by the post-hook, ADR 0033). Each pair below asks one question
two ways, on the same data:

- **initial:** aggregate the fact table (every award or invoice row);
- **optimized:** read the pre-aggregated mart.

Both must return the same answer (fingerprint), so the mart is a faithful optimization. Bytes
scanned and engine time come from Athena's own statistics, with result reuse off, and are
written to ``build/evidence/performance_acceptance.json``. The test asserts only that the
optimized query reads no more than the initial one; it reports the measured sizes and times,
and makes no claim beyond them.

Partition pruning is not one of the pairs: by the owner's decision (OQ13, ADR 0034) Silver is
partitioned by ``tenant_id`` only, so date filters do not prune in the current layout. The
pruning experiment's measurements are in ``docs/benchmarks/2026-11-11-bench-100k-1m.md``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procurelake.benchmarks.athena_queries import fingerprint, run_record
from procurelake.common.athena import run_query

pytestmark = pytest.mark.aws

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "build" / "evidence" / "performance_acceptance.json"

# (question, initial over the fact, optimized over the mart). `{gold}` is the database under test.
PAIRS = [
    (
        "monthly awarded value (active awards, by currency)",
        """select award_month, currency_code, count(*) as awards, sum(award_amount) as awarded_value
           from {gold}.fct_contract_award where award_status = 'active'
           group by 1, 2 order by 1, 2""",
        """select award_month, currency_code, sum(award_count) as awards, sum(awarded_value) as awarded_value
           from {gold}.mart_awarded_value
           group by 1, 2 order by 1, 2""",
    ),
    (
        "monthly actual spend (paid invoices, by currency)",
        """select paid_month, currency_code, count(*) as invoices, sum(paid_amount) as actual_spend
           from {gold}.fct_invoice where paid_amount > 0
           group by 1, 2 order by 1, 2""",
        """select paid_month, currency_code, sum(paid_invoice_count) as invoices,
                  sum(actual_spend) as actual_spend
           from {gold}.mart_actual_spend
           group by 1, 2 order by 1, 2""",
    ),
]


@pytest.fixture(scope="module")
def bench(engineer_session, dev_env):
    gold = os.environ.get("PROCURELAKE_DEV_BENCH_GOLD_DATABASE")
    if not gold or not gold.replace("_", "").isalnum():
        pytest.fail("missing or invalid PROCURELAKE_DEV_BENCH_GOLD_DATABASE (use `make test-aws`)")
    record = {"started_at": datetime.now(UTC).isoformat(), "gold_database": gold, "pairs": []}
    yield {"athena": engineer_session.client("athena"), "wg": dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
           "gold": gold, "record": record}  # fmt: skip
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(record, indent=2) + "\n")


def _measure(bench, sql: str) -> tuple[str, int, dict]:
    result = run_query(bench["athena"], sql.format(gold=bench["gold"]), bench["wg"], bench["gold"])
    record = run_record(result)
    assert not record["reused_result"], "Athena reused a result; not a measurement"
    return fingerprint(result.columns, result.rows), len(result.rows), record


@pytest.mark.parametrize(("question", "initial_sql", "optimized_sql"), PAIRS, ids=[p[0] for p in PAIRS])
def test_optimized_query_gives_the_same_answer_and_reads_no_more(bench, question, initial_sql, optimized_sql):
    initial_print, rows, initial = _measure(bench, initial_sql)
    optimized_print, _, optimized = _measure(bench, optimized_sql)
    bench["record"]["pairs"].append({
        "question": question, "answer_rows": rows, "initial": initial, "optimized": optimized,
        "scanned_ratio": round(optimized["scanned_bytes"] / initial["scanned_bytes"], 4)
        if initial["scanned_bytes"] else None,
    })  # fmt: skip
    assert rows > 0, f"{question}: the bench Gold database is empty"
    assert optimized_print == initial_print, f"{question}: the mart's answer differs from the fact's"
    assert optimized["scanned_bytes"] <= initial["scanned_bytes"]
