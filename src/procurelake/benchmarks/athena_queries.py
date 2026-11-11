"""Run the analytical query benchmark on Athena and compare two runs (ADR 0034).

Each query in ``configs/benchmarks/athena_queries.yaml`` runs ``repeats`` times in sequence.
Athena's own ``Statistics`` are recorded for every run (engine, planning, queue and total time,
bytes scanned, result reuse), together with a SHA-256 of the result rows. Two runs over different
layouts of the same data must return identical results, so ``compare`` fails on a fingerprint
mismatch rather than reporting a speed-up for a different answer.

The summary per query is the median and the min/max over the repeats: with a handful of runs a
percentile would claim more than was measured.
"""

from __future__ import annotations

import hashlib
import re
import statistics as stats
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from procurelake.common.athena import run_query

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class BenchQuery:
    name: str
    layer: str
    purpose: str
    sql: str
    prune_on: str | None = None


def load_queries(path: Path) -> list[BenchQuery]:
    doc = yaml.safe_load(path.read_text())
    if doc.get("version") != 1:
        raise ValueError(f"{path}: unsupported version {doc.get('version')!r}")
    queries = [BenchQuery(**q) for q in doc["queries"]]
    names = [q.name for q in queries]
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: duplicate query names")
    for q in queries:
        if q.layer not in {"silver", "gold"}:
            raise ValueError(f"{q.name}: layer must be silver or gold")
    return queries


def render(query: BenchQuery, silver: str, gold: str) -> str:
    for name in (silver, gold):
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"unsafe database name: {name!r}")
    return query.sql.format(silver=silver, gold=gold).strip()


def fingerprint(columns: list[str], rows: list[tuple]) -> str:
    h = hashlib.sha256("\x1f".join(columns).encode())
    for row in rows:  # queries are ORDER BY'd, so row order is part of the answer
        h.update(b"\x1e" + "\x1f".join("\x00" if v is None else str(v) for v in row).encode())
    return h.hexdigest()


def _run_record(result) -> dict:
    s = result.statistics
    return {
        "query_id": result.query_execution_id,
        "engine_ms": int(s.get("EngineExecutionTimeInMillis", 0)),
        "total_ms": int(s.get("TotalExecutionTimeInMillis", 0)),
        "planning_ms": int(s.get("QueryPlanningTimeInMillis", 0)),
        "queue_ms": int(s.get("QueryQueueTimeInMillis", 0)),
        "scanned_bytes": int(s.get("DataScannedInBytes", 0)),
        "reused_result": bool(s.get("ResultReuseInformation", {}).get("ReusedPreviousResult", False)),
    }


def _summary(runs: list[dict]) -> dict:
    out = {}
    for key in ("engine_ms", "total_ms", "planning_ms", "scanned_bytes"):
        values = [r[key] for r in runs]
        out[key] = {"median": stats.median(values), "min": min(values), "max": max(values)}
    return out


def run_benchmark(
    athena,
    workgroup: str,
    queries: list[BenchQuery],
    silver: str,
    gold: str,
    repeats: int,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    started = datetime.now(UTC)
    results = []
    for q in queries:
        sql = render(q, silver, gold)
        database = silver if q.layer == "silver" else gold
        runs, prints, row_count = [], set(), 0
        for _ in range(repeats):
            r = run_query(athena, sql, workgroup, database, sleep=sleep)
            runs.append(_run_record(r))
            prints.add(fingerprint(r.columns, r.rows))
            row_count = len(r.rows)
        if len(prints) != 1:
            raise RuntimeError(f"{q.name}: results differed between repeats; the data changed mid-run")
        if any(run["reused_result"] for run in runs):
            raise RuntimeError(f"{q.name}: Athena reused a previous result; the timing is not a measurement")
        results.append(
            {
                "name": q.name,
                "layer": q.layer,
                "prune_on": q.prune_on,
                "sql": sql,
                "rows": row_count,
                "fingerprint": prints.pop(),
                "runs": runs,
                "summary": _summary(runs),
            }
        )
    return {
        "label": label,
        "workgroup": workgroup,
        "silver_database": silver,
        "gold_database": gold,
        "repeats": repeats,
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "queries": results,
    }


def compare(before: dict, after: dict) -> list[dict]:
    """Per query: medians before and after. Raises if any answer changed."""
    b = {q["name"]: q for q in before["queries"]}
    rows = []
    for q in after["queries"]:
        if q["name"] not in b:
            continue
        p = b[q["name"]]
        if p["fingerprint"] != q["fingerprint"]:
            raise ValueError(f"{q['name']}: results differ between {before['label']} and {after['label']}")
        rows.append(
            {
                "name": q["name"],
                "prune_on": q["prune_on"],
                "scanned_before": p["summary"]["scanned_bytes"]["median"],
                "scanned_after": q["summary"]["scanned_bytes"]["median"],
                "engine_ms_before": p["summary"]["engine_ms"]["median"],
                "engine_ms_after": q["summary"]["engine_ms"]["median"],
                "total_ms_before": p["summary"]["total_ms"]["median"],
                "total_ms_after": q["summary"]["total_ms"]["median"],
            }
        )
    return rows


def markdown(rows: list[dict], before_label: str, after_label: str) -> str:
    head = (
        f"| Query | Prunes on | Scanned {before_label} | Scanned {after_label} | "
        f"Engine ms {before_label} | Engine ms {after_label} |\n|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| `{r['name']}` | {r['prune_on'] or 'n/a'} | {r['scanned_before']:,.0f} B | "
        f"{r['scanned_after']:,.0f} B | {r['engine_ms_before']:,.0f} | {r['engine_ms_after']:,.0f} |\n"
        for r in rows
    )
    return head + body
