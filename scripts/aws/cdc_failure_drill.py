"""Failure and recovery drill for scheduled CDC processing (Week 6 / C5 retries; ADR 0029).

1. Hold the ``silver_invoices`` CDC lease as another writer would (``CheckpointStore``, the
   same code the job uses), for ``--hold-seconds``.
2. Apply a small workload, so invoices have new Bronze files that cannot be applied.
3. Wait for a scheduled ``cdc_processing`` execution to FAIL: the CDC apply raises on the held
   lease, Step Functions retries once, then publishes the SNS alert and ends in ``Fail``.
4. Wait for the first scheduled execution that starts after the lease expires to SUCCEED.
5. Check the recovery: every drill change is in Silver and Gold, the invoices checkpoint moved,
   no lease is left, and the alert topic published at least one message in the window.

Evidence: build/evidence/cdc_failure_drill_<start>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.cdc.verify import check_layers
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.data_generator.workload import run_workload

EVIDENCE = Path(__file__).resolve().parents[2] / "build" / "evidence"


def _executions(sfn, arn: str, since: datetime) -> list[dict]:
    pages = sfn.get_paginator("list_executions").paginate(stateMachineArn=arn)
    return sorted(
        (e for p in pages for e in p["executions"] if e["startDate"] >= since), key=lambda e: e["startDate"]
    )


def _ran_apply(sfn, execution_arn: str) -> bool:
    """True unless the execution only counted executions and skipped (an overlap skip)."""
    events = sfn.get_execution_history(executionArn=execution_arn, maxResults=200)["events"]
    return any(
        e["type"] == "TaskStateExited" and e["stateExitedEventDetails"]["name"] == "ApplyCdc" for e in events
    )


def _wait_for(sfn, arn, since, status, timeout_s, poll_s=20) -> dict:
    # Only executions that ran the CDC apply count: a 5-minute tick that finds another
    # execution running succeeds immediately without doing anything (live drill, 2026-11-10).
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for e in _executions(sfn, arn, since):
            if e["status"] == status and _ran_apply(sfn, e["executionArn"]):
                return e
        time.sleep(poll_s)
    raise TimeoutError(f"no {status} execution of {arn} since {since.isoformat()} within {timeout_s}s")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    required = [
        "host", "database", "master-secret-arn", "state-machine-arn", "checkpoint-table",
        "alerts-topic-arn", "engineer-role-arn", "workgroup", "silver-database", "gold-database",
    ]  # fmt: skip
    for name in required:
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--seed", type=int, default=21)
    p.add_argument("--operations", type=int, default=15)
    p.add_argument("--hold-seconds", type=int, default=420)
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)

    session = build_session(region=args.region)
    sfn, cw = session.client("stepfunctions"), session.client("cloudwatch")
    checkpoints = CheckpointStore(session.client("dynamodb"), args.checkpoint_table)
    master = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=args.master_secret_arn)["SecretString"]
    )
    dsn = psycopg.conninfo.make_conninfo(
        host=args.host, port=args.port, dbname=args.database, user=master["username"],
        password=master["password"], sslmode="require", connect_timeout=15,
    )  # fmt: skip

    started = datetime.now(UTC)
    before = checkpoints.checkpoint("silver_invoices")
    lease = checkpoints.acquire_lease("silver_invoices", f"drill:{started:%Y%m%dT%H%M%SZ}", args.hold_seconds)
    released_at = datetime.fromtimestamp(lease.expires_at, UTC)
    print(f"holding silver_invoices lease until {released_at.isoformat()}", flush=True)
    with psycopg.connect(dsn) as conn:
        workload = run_workload(conn, seed=args.seed, operations=args.operations)
    print(f"drill workload committed {len(workload.changes)} row changes", flush=True)

    failed = _wait_for(sfn, args.state_machine_arn, started, "FAILED", args.hold_seconds + 600)
    print(f"failed execution: {failed['name']} at {failed['startDate'].isoformat()}", flush=True)
    history = sfn.get_execution_history(executionArn=failed["executionArn"], maxResults=200)["events"]
    task_failures = sum(1 for e in history if e["type"] == "TaskFailed")
    notified = any(e["type"] == "TaskSucceeded" and "sns" in json.dumps(e, default=str) for e in history)

    # The recovery can start before the lease expires (it reaches silver_invoices later), so
    # look for the first applying success that started after the failed execution did.
    recovered = _wait_for(sfn, args.state_machine_arn, failed["startDate"], "SUCCEEDED", 1200)
    print(f"recovered by: {recovered['name']} at {recovered['startDate'].isoformat()}", flush=True)
    time.sleep(5)
    after = checkpoints.checkpoint("silver_invoices")

    athena = build_session(region=args.region, role_arn=args.engineer_role_arn, session_name="drill").client(
        "athena"
    )
    silver_bad, gold_bad = check_layers(
        athena, args.workgroup, args.silver_database, args.gold_database, workload.changes
    )
    leases = session.client("dynamodb").scan(
        TableName=args.checkpoint_table,
        FilterExpression="sk = :l",
        ExpressionAttributeValues={":l": {"S": "lease"}},
        Select="COUNT",
    )["Count"]
    topic = args.alerts_topic_arn.rsplit(":", 1)[-1]
    published = cw.get_metric_statistics(
        Namespace="AWS/SNS",
        MetricName="NumberOfMessagesPublished",
        Dimensions=[{"Name": "TopicName", "Value": topic}],
        StartTime=started - timedelta(minutes=5),
        EndTime=datetime.now(UTC) + timedelta(minutes=1),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    record = {
        "started_at": started.isoformat(),
        "lease_held_until": released_at.isoformat(),
        "drill_changes": len(workload.changes),
        "failed_execution": {
            "name": failed["name"],
            "start": failed["startDate"],
            "stop": failed.get("stopDate"),
            "task_failed_events": task_failures,
            "sns_publish_succeeded": notified,
        },
        "recovered_execution": {
            "name": recovered["name"],
            "start": recovered["startDate"],
            "stop": recovered.get("stopDate"),
        },
        "checkpoint_before": before,
        "checkpoint_after": after,
        "leases_left": leases,
        "sns_messages_published_in_window": sum(d["Sum"] for d in published),
        "silver_mismatches": silver_bad,
        "gold_mismatches": gold_bad,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"cdc_failure_drill_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps(record, indent=2, default=str))
    ok = (
        task_failures >= 2  # the first attempt and its retry
        and notified
        and not silver_bad
        and not gold_bad
        and leases == 0
        and after
        and (before is None or after["batch_id"] != before["batch_id"])
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
