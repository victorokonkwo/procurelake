"""Measure the C5 freshness gate end to end, live, and verify every change landed correctly.

1. Apply the CDC workload to the RDS source (commit time recorded per change).
2. Wait until AWS DMS reports every change (table statistics: inserts/updates/deletes deltas).
3. Run the CDC apply Glue job (with the DMS task ARN) -> Silver available at its CompletedOn.
4. Run ``make dbt-build`` -> Gold available when it returns.
5. Check, for every changed key, its final state in Silver and in Gold (Athena, data-engineer
   role): deletes are tombstones in Silver and absent from Gold; other changes show the
   after-image value.
6. Report latency percentiles per layer against the gate (procurelake.cdc.freshness).

Evidence: build/evidence/cdc_freshness_<start>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from procurelake.cdc.freshness import latency_summary
from procurelake.common.athena import run_query
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.glue_runs import start_and_wait
from procurelake.data_generator.workload import run_workload

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "build" / "evidence"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# table -> (primary key, Silver value column, Gold model, Gold value column)
CHECKS = {
    "invoices": ("invoice_id", "payment_status", "fct_invoice", "payment_status"),
    "awards": ("award_id", "status", "fct_contract_award", "award_status"),
    "suppliers": ("supplier_id", "supplier_name", "dim_supplier", "supplier_name"),
    "purchase_orders": ("po_id", "status", "fct_purchase_order", "po_status"),
}


def _dms_counts(dms, task_arn: str) -> dict[str, int]:
    stats = dms.describe_table_statistics(ReplicationTaskArn=task_arn)["TableStatistics"]
    return {s["TableName"]: s["Inserts"] + s["Updates"] + s["Deletes"] for s in stats}


def _keys_sql(keys: list[str]) -> str:
    for k in keys:
        if not _UUID.match(k):
            raise ValueError(f"refusing to place {k!r} in SQL")
    return ", ".join(f"'{k}'" for k in keys)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("host", "database", "master-secret-arn", "task-arn", "glue-job", "engineer-role-arn",
                 "workgroup", "silver-database", "gold-database"):  # fmt: skip
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--operations", type=int, default=40)
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)

    session = build_session(region=args.region)
    dms = session.client("dms")
    master = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=args.master_secret_arn)["SecretString"]
    )
    dsn = psycopg.conninfo.make_conninfo(
        host=args.host, port=args.port, dbname=args.database, user=master["username"],
        password=master["password"], sslmode="require", connect_timeout=15,
    )  # fmt: skip

    started = datetime.now(UTC)
    baseline = _dms_counts(dms, args.task_arn)
    with psycopg.connect(dsn) as conn:
        workload = run_workload(conn, seed=args.seed, operations=args.operations)
    expected: dict[str, int] = {}
    for c in workload.changes:
        expected[c.table] = expected.get(c.table, 0) + 1
    print(f"workload committed {len(workload.changes)} row changes: {expected}", flush=True)

    deadline = time.monotonic() + 900
    while True:
        now = _dms_counts(dms, args.task_arn)
        if all(now.get(t, 0) - baseline.get(t, 0) >= n for t, n in expected.items()):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"DMS did not report all changes within 15 minutes: {now} vs {baseline}")
        time.sleep(10)
    dms_seen = datetime.now(UTC)
    print(f"DMS reported all changes at {dms_seen.isoformat()}", flush=True)

    glue = start_and_wait(
        session.client("glue"), args.glue_job, {"--dms-task-arn": args.task_arn}, poll_seconds=10
    )
    if not glue.succeeded:
        raise RuntimeError(f"CDC apply failed: {glue}")
    silver_at = glue.completed_on
    print(f"Silver available at {silver_at.isoformat()} (run {glue.run_id})", flush=True)

    subprocess.run(["make", "dbt-build"], cwd=REPO, check=True)  # noqa: S607 - fixed command
    gold_at = datetime.now(UTC)
    print(f"Gold available at {gold_at.isoformat()}", flush=True)

    athena = build_session(
        region=args.region, role_arn=args.engineer_role_arn, session_name="freshness"
    ).client("athena")
    latest = {}
    for c in workload.changes:
        latest[(c.table, c.key)] = c
    mismatches: list[str] = []
    for table, (pk, silver_col, gold_model, gold_col) in CHECKS.items():
        changes = {k: c for (t, k), c in latest.items() if t == table}
        if not changes:
            continue
        keys = _keys_sql(sorted(changes))
        silver = {
            r[0]: (r[1], r[2])
            for r in run_query(
                athena,
                f"SELECT {pk}, {silver_col}, coalesce(_is_deleted, false) "  # noqa: S608
                f"FROM silver_{table} WHERE {pk} IN ({keys})",
                args.workgroup,
                args.silver_database,
            ).rows
        }
        gold = {
            r[0]: r[1]
            for r in run_query(
                athena,
                f"SELECT {pk}, {gold_col} FROM {gold_model} WHERE {pk} IN ({keys})",  # noqa: S608
                args.workgroup,
                args.gold_database,
            ).rows
        }
        for key, change in changes.items():
            if change.kind == "D":
                if silver.get(key, (None, "false"))[1] != "true":
                    mismatches.append(f"silver_{table} {key}: not a tombstone")
                if key in gold:
                    mismatches.append(f"{gold_model} {key}: deleted key still in Gold")
                continue
            want = str(change.after[silver_col])
            if silver.get(key) != (want, "false"):
                mismatches.append(f"silver_{table} {key}: {silver.get(key)} != {(want, 'false')}")
            if gold.get(key) != want:
                mismatches.append(f"{gold_model} {key}: {gold.get(key)} != {want}")

    commits = [c.committed_at for c in workload.changes]
    record = {
        "started_at": started.isoformat(),
        "seed": args.seed,
        "operations": args.operations,
        "row_changes": len(workload.changes),
        "by_table": expected,
        "dms_reported_all_at": dms_seen.isoformat(),
        "glue_run": {
            "run_id": glue.run_id,
            "seconds": glue.execution_time_seconds,
            "dpu_seconds": glue.dpu_seconds,
            "completed_on": silver_at.isoformat(),
        },
        "gold_available_at": gold_at.isoformat(),
        "silver": latency_summary(commits, silver_at),
        "gold": latency_summary(commits, gold_at),
        "mismatches": mismatches,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"cdc_freshness_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps({k: record[k] for k in ("silver", "gold", "mismatches")}, indent=2))
    return 0 if not mismatches and record["gold"]["meets_gate"] and record["silver"]["meets_gate"] else 1


if __name__ == "__main__":
    sys.exit(main())
