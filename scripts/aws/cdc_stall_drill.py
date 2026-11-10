"""Week 8 incident drill: a silent DMS stall (implementation plan E; owner decision 2026-11-10).

A stopped replication task is a failure the pipeline cannot report itself. The 5-minute
``cdc_processing`` schedule keeps succeeding with nothing new to apply, and Silver and Gold go
stale. This drill shows that monitoring catches it and that the pipeline recovers from DMS's own
checkpoint without losing or duplicating changes.

1. Stop the DMS task (``StopReplicationTask``), as an operator mistake or an outage would.
2. Commit a workload on the source **during** the stall: these changes cannot reach Bronze.
3. Detection: wait for the ``dms-source-latency`` alarm to enter ALARM. Meanwhile, record that
   scheduled ``cdc_processing`` executions still SUCCEED and that the stall changes are absent
   from Silver (the failure is silent to the pipeline).
4. Recovery: ``StartReplicationTask`` with ``resume-processing`` (DMS resumes from the point it
   stopped, its own checkpoint), then wait until every stall change is correct in Silver and in
   Gold (scheduled runs only, nothing applied by hand).
5. Wait for the alarm to return to OK, and count alert messages in the window.

Evidence: build/evidence/cdc_stall_drill_<start>.json. Synthetic data only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from procurelake.cdc.verify import check_layers
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.data_generator.workload import run_workload

EVIDENCE = Path(__file__).resolve().parents[2] / "build" / "evidence"


def _now() -> datetime:
    return datetime.now(UTC)


def _task_status(dms, task_arn: str) -> str:
    task = dms.describe_replication_tasks(Filters=[{"Name": "replication-task-arn", "Values": [task_arn]}])
    return task["ReplicationTasks"][0]["Status"]


def _wait(predicate, what: str, timeout_s: int, poll_s: int = 20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll_s)
    raise TimeoutError(f"timed out after {timeout_s}s waiting for {what}")


def _alarm_state(cw, name: str) -> str:
    return cw.describe_alarms(AlarmNames=[name])["MetricAlarms"][0]["StateValue"]


def _executions(sfn, arn: str, since: datetime) -> list[dict]:
    pages = sfn.get_paginator("list_executions").paginate(stateMachineArn=arn)
    return [e for p in pages for e in p["executions"] if e["startDate"] >= since]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    required = [
        "host", "database", "master-secret-arn", "task-arn", "alarm-name", "state-machine-arn",
        "alerts-topic-arn", "engineer-role-arn", "workgroup", "silver-database", "gold-database",
    ]  # fmt: skip
    for name in required:
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--seed", type=int, default=31)
    p.add_argument("--operations", type=int, default=25)
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)

    session = build_session(region=args.region)
    dms, cw, sfn = session.client("dms"), session.client("cloudwatch"), session.client("stepfunctions")
    athena = build_session(
        region=args.region, role_arn=args.engineer_role_arn, session_name="stall-drill"
    ).client("athena")
    secret = session.client("secretsmanager").get_secret_value(SecretId=args.master_secret_arn)[
        "SecretString"
    ]
    master = json.loads(secret)
    dsn = psycopg.conninfo.make_conninfo(
        host=args.host, port=args.port, dbname=args.database, user=master["username"],
        password=master["password"], sslmode="require", connect_timeout=15,
    )  # fmt: skip
    layers = (athena, args.workgroup, args.silver_database, args.gold_database)

    if _task_status(dms, args.task_arn) != "running":
        raise SystemExit("the DMS task must be running before the drill")
    if _alarm_state(cw, args.alarm_name) != "OK":
        raise SystemExit(f"{args.alarm_name} must be OK before the drill (it is not)")

    started = _now()
    record: dict = {"started_at": started.isoformat()}

    # 1. Stall.
    dms.stop_replication_task(ReplicationTaskArn=args.task_arn)
    _wait(lambda: _task_status(dms, args.task_arn) == "stopped", "task stopped", 600, 10)
    record["task_stopped_at"] = _now().isoformat()
    print(f"DMS task stopped at {record['task_stopped_at']}", flush=True)

    # 2. Changes the stall withholds.
    with psycopg.connect(dsn) as conn:
        workload = run_workload(conn, seed=args.seed, operations=args.operations)
    record["stall_changes"] = len(workload.changes)
    record["stall_changes_committed_at"] = max(c.committed_at for c in workload.changes).isoformat()
    print(f"committed {len(workload.changes)} changes during the stall", flush=True)

    # 3. Detection.
    _wait(lambda: _alarm_state(cw, args.alarm_name) == "ALARM", "the latency alarm", 1800)
    detected = _now()
    record["alarm_at"] = detected.isoformat()
    record["time_to_detect_s"] = round(
        (detected - datetime.fromisoformat(record["task_stopped_at"])).total_seconds()
    )
    during = _executions(sfn, args.state_machine_arn, datetime.fromisoformat(record["task_stopped_at"]))
    record["scheduled_runs_during_stall"] = {
        s: sum(1 for e in during if e["status"] == s) for s in ("SUCCEEDED", "FAILED", "RUNNING")
    }
    silver_bad, _gold_bad = check_layers(*layers, workload.changes)
    record["stall_changes_missing_from_silver_at_detection"] = len(silver_bad)
    runs = record["scheduled_runs_during_stall"]
    print(f"alarm after {record['time_to_detect_s']} s; scheduled runs during the stall: {runs}", flush=True)

    # 4. Recovery from DMS's checkpoint.
    dms.start_replication_task(ReplicationTaskArn=args.task_arn, StartReplicationTaskType="resume-processing")
    _wait(lambda: _task_status(dms, args.task_arn) == "running", "task running", 900, 15)
    resumed = _now()
    record["task_resumed_at"] = resumed.isoformat()

    def caught_up():
        silver, gold = check_layers(*layers, workload.changes)
        return not silver and not gold

    _wait(caught_up, "every stall change in Silver and Gold", 2700, 60)
    recovered = _now()
    record["all_changes_in_gold_at"] = recovered.isoformat()
    record["resume_to_gold_s"] = round((recovered - resumed).total_seconds())
    record["stale_window_s"] = round(
        (recovered - datetime.fromisoformat(record["stall_changes_committed_at"])).total_seconds()
    )

    # 5. All clear.
    _wait(lambda: _alarm_state(cw, args.alarm_name) == "OK", "the alarm to clear", 1800)
    record["alarm_ok_at"] = _now().isoformat()
    time.sleep(60)
    published = cw.get_metric_statistics(
        Namespace="AWS/SNS",
        MetricName="NumberOfMessagesPublished",
        Dimensions=[{"Name": "TopicName", "Value": args.alerts_topic_arn.rsplit(":", 1)[-1]}],
        StartTime=started - timedelta(minutes=1),
        EndTime=_now() + timedelta(minutes=1),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    record["sns_messages_published_in_window"] = sum(d["Sum"] for d in published)
    after = _executions(sfn, args.state_machine_arn, started)
    record["cdc_processing_failed_in_window"] = sum(1 for e in after if e["status"] == "FAILED")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"cdc_stall_drill_{started:%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps(record, indent=2, default=str))
    ok = (
        record["stall_changes_missing_from_silver_at_detection"] > 0
        and record["scheduled_runs_during_stall"]["SUCCEEDED"] > 0
        and record["sns_messages_published_in_window"] >= 2  # ALARM and OK at least
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
