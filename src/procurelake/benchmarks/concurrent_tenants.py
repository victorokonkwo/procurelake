"""Concurrent tenant access benchmark (implementation plan G, "two tenant query sessions").

Each fictional tenant (``tenant_alder``, ``tenant_birch``) runs the same mart queries as its own
role in its own Athena workgroup, through Lake Formation row filters. Two phases:

1. **sequential:** one tenant's whole round, then the other's (baseline latency);
2. **concurrent:** both tenants' rounds at the same time, one thread per tenant.

For every query the result must hold only the querying tenant's ``tenant_id`` (isolation under
concurrency), and both phases must return the same answer per tenant and query. Latency is
Athena's own statistics (engine, queue, total), never wall-clock guesses.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from procurelake.benchmarks.athena_queries import fingerprint, run_record
from procurelake.common.athena import run_query

MARTS: tuple[str, ...] = (
    "mart_awarded_value",
    "mart_actual_spend",
    "mart_procurement_cycle_time",
    "mart_supplier_concentration",
)


def tenant_sql(gold: str, mart: str) -> str:
    if not gold.replace("_", "").isalnum() or mart not in MARTS:
        raise ValueError(f"unsafe identifier: {gold}.{mart}")
    # Identifiers are validated above (fixed mart list, alphanumeric database name).
    return f"select tenant_id, count(*) as row_count from {gold}.{mart} group by 1 order by 1"  # noqa: S608


class IsolationBreach(RuntimeError):
    """A tenant query returned another tenant's rows."""


def run_round(
    athena: Any, tenant: str, workgroup: str, gold: str, rounds: int, sleep: Callable[[float], None]
) -> list[dict]:
    out = []
    for i in range(rounds):
        for mart in MARTS:
            r = run_query(athena, tenant_sql(gold, mart), workgroup, gold, sleep=sleep)
            seen = {row[0] for row in r.rows}
            if seen - {tenant}:
                raise IsolationBreach(f"{tenant} saw {sorted(seen - {tenant})} in {mart}")
            out.append(
                {
                    "tenant": tenant,
                    "round": i,
                    "mart": mart,
                    "rows": [list(row) for row in r.rows],
                    "fingerprint": fingerprint(r.columns, r.rows),
                    **run_record(r),
                }
            )
    return out


def _summary(records: list[dict]) -> dict:
    def stat(key: str) -> dict:
        values = [r[key] for r in records]
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}

    return {
        "queries": len(records),
        "engine_ms": stat("engine_ms"),
        "queue_ms": stat("queue_ms"),
        "total_ms": stat("total_ms"),
    }


def run_benchmark(
    clients: Mapping[str, Any],
    workgroups: Mapping[str, str],
    gold: str,
    rounds: int = 3,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """``clients`` maps each tenant to an Athena client created from that tenant's role."""
    sleep = sleep or time.sleep
    tenants = sorted(clients)
    started = datetime.now(UTC)

    sequential: list[dict] = []
    for t in tenants:
        sequential += run_round(clients[t], t, workgroups[t], gold, rounds, sleep)

    with ThreadPoolExecutor(max_workers=len(tenants)) as pool:
        futures = [pool.submit(run_round, clients[t], t, workgroups[t], gold, rounds, sleep) for t in tenants]
        concurrent = [rec for f in futures for rec in f.result()]

    answers: dict[tuple[str, str], set[str]] = {}
    for rec in sequential + concurrent:
        answers.setdefault((rec["tenant"], rec["mart"]), set()).add(rec["fingerprint"])
    differing = sorted(f"{t}/{m}" for (t, m), prints in answers.items() if len(prints) != 1)
    if differing:
        raise RuntimeError(f"answers changed between runs (data changed mid-run?): {differing}")
    if any(r["reused_result"] for r in sequential + concurrent):
        raise RuntimeError("Athena reused a previous result; the timing is not a measurement")

    return {
        "started_at": started.isoformat(),
        "gold_database": gold,
        "tenants": tenants,
        "rounds": rounds,
        "isolation": "every result held only the querying tenant's rows",
        "summary": {
            phase: {t: _summary([r for r in recs if r["tenant"] == t]) for t in tenants}
            for phase, recs in (("sequential", sequential), ("concurrent", concurrent))
        },
        "runs": {"sequential": sequential, "concurrent": concurrent},
    }
