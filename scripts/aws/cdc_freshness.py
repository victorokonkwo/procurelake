"""Measure the C5 freshness gate end to end, live, and verify every change landed correctly.

1. Apply the CDC workload to the RDS source (commit time recorded per change).
2. Wait until AWS DMS reports every change (table statistics: inserts/updates/deletes deltas).
3. Run the CDC apply Glue job (with the DMS task ARN) -> Silver available at its CompletedOn.
4. Run ``make dbt-build`` -> Gold available when it returns.
5. Check, for every changed key, its final state in Silver and in Gold (Athena, data-engineer
   role): deletes are tombstones in Silver and absent from Gold; other changes show the
   after-image value.
6. Report latency percentiles per layer against the gate (procurelake.cdc.freshness).

With ``--scheduled``, steps 2-4 are left to the EventBridge schedule (ADR 0029): the harness
only applies the workload and polls Silver and Gold every ``--poll-seconds`` until every change
is visible. Latency is then measured to the first observation (resolution: the poll interval).

Evidence: build/evidence/cdc_freshness[_scheduled]_<start>.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from procurelake.cdc.freshness import latency_summary
from procurelake.cdc.verify import check_layers
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.glue_runs import start_and_wait
from procurelake.data_generator.workload import run_workload

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "build" / "evidence"


def _dms_counts(dms, task_arn: str) -> dict[str, int]:
    stats = dms.describe_table_statistics(ReplicationTaskArn=task_arn)["TableStatistics"]
    return {s["TableName"]: s["Inserts"] + s["Updates"] + s["Deletes"] for s in stats}


def observe(athena, args, workload) -> tuple[datetime, datetime, list[str]]:
    """Scheduled mode: start nothing; poll until each layer shows every change."""
    silver_at = gold_at = None
    deadline = time.monotonic() + args.max_wait_seconds
    while True:
        checked_at = datetime.now(UTC)
        silver_bad, gold_bad = check_layers(
            athena, args.workgroup, args.silver_database, args.gold_database, workload.changes
        )
        if silver_at is None and not silver_bad:
            silver_at = checked_at
            print(f"Silver shows every change at {silver_at.isoformat()}", flush=True)
        if gold_at is None and not gold_bad and silver_at is not None:
            gold_at = checked_at
            print(f"Gold shows every change at {gold_at.isoformat()}", flush=True)
            return silver_at, gold_at, []
        if time.monotonic() > deadline:
            return silver_at, gold_at, silver_bad + gold_bad
        time.sleep(args.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("host", "database", "master-secret-arn", "task-arn", "glue-job", "engineer-role-arn",
                 "workgroup", "silver-database", "gold-database"):  # fmt: skip
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--operations", type=int, default=40)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="Start nothing (the schedule runs the pipeline); poll Silver/Gold until visible.",
    )
    p.add_argument("--poll-seconds", type=float, default=30)
    p.add_argument("--max-wait-seconds", type=float, default=1800)
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
    athena = build_session(
        region=args.region, role_arn=args.engineer_role_arn, session_name="freshness"
    ).client("athena")
    if args.scheduled:
        return _scheduled(args, workload, expected, started, athena)

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

    silver_bad, gold_bad = check_layers(
        athena, args.workgroup, args.silver_database, args.gold_database, workload.changes
    )
    mismatches = silver_bad + gold_bad

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


def _scheduled(args, workload, expected, started, athena) -> int:
    silver_at, gold_at, mismatches = observe(athena, args, workload)
    commits = [c.committed_at for c in workload.changes]
    record = {
        "mode": "scheduled (pipeline started by EventBridge Scheduler; visibility observed by polling)",
        "observed_every_s": args.poll_seconds,
        "started_at": started.isoformat(),
        "seed": args.seed,
        "row_changes": len(workload.changes),
        "by_table": expected,
        "silver_visible_at": silver_at.isoformat() if silver_at else None,
        "gold_visible_at": gold_at.isoformat() if gold_at else None,
        "silver": latency_summary(commits, silver_at) if silver_at else None,
        "gold": latency_summary(commits, gold_at) if gold_at else None,
        "mismatches": mismatches,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"cdc_freshness_scheduled_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps({k: record[k] for k in ("silver", "gold", "mismatches")}, indent=2))
    ok = not mismatches and record["gold"] and record["gold"]["meets_gate"] and record["silver"]["meets_gate"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
