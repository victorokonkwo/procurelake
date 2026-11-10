"""Failure drill for the tenant access check (ADR 0030): make one tenant see another's rows,
prove the check fails and alerts, then restore isolation and prove the check passes again.

1. Grant the tenant an UNFILTERED ``SELECT`` on one mart (Lake Formation, outside Terraform).
   Together with its filtered grant, the tenant now sees every tenant's rows in that mart.
2. Run ``tenant_access_check``: it must FAIL, with the ``ForeignRowsVisible`` error visible in
   the execution history, and the alerts topic must publish a message.
3. Revoke the grant. This always runs, even if step 2 errors.
4. Run the check again: it must SUCCEED.

The tenants are fictional and synthetic. Evidence: build/evidence/tenant_access_drill_<start>.json.
Afterwards ``terraform plan`` on dev/security must show no changes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.sfn_runs import start_and_wait

EVIDENCE = Path(__file__).resolve().parents[2] / "build" / "evidence"


def _grant_args(account: str, role_arn: str, database: str, table: str) -> dict:
    return {
        "CatalogId": account,
        "Principal": {"DataLakePrincipalIdentifier": role_arn},
        "Resource": {"Table": {"CatalogId": account, "DatabaseName": database, "Name": table}},
        "Permissions": ["SELECT"],
    }


def _failed_states(sfn, execution_arn: str) -> list[str]:
    pages = sfn.get_paginator("get_execution_history").paginate(executionArn=execution_arn)
    errors = []
    for page in pages:
        for e in page["events"]:
            for key in ("executionFailedEventDetails", "mapIterationFailedEventDetails"):
                if key in e and e[key].get("error"):
                    errors.append(e[key]["error"])
            if e["type"] == "FailStateEntered":
                errors.append(e["stateEnteredEventDetails"]["name"])
    return errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("account-id", "check-arn", "tenant-role-arn", "alerts-topic-arn", "database"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--table", default="mart_supplier_concentration")
    p.add_argument("--settle-seconds", type=int, default=20)
    p.add_argument("--region", default=DEFAULT_REGION)
    args = p.parse_args(argv)

    session = build_session(region=args.region)
    lf = session.client("lakeformation")
    sfn = session.client("stepfunctions")
    cw = session.client("cloudwatch")
    grant = _grant_args(args.account_id, args.tenant_role_arn, args.database, args.table)
    started = datetime.now(UTC)
    stamp = f"{started:%Y%m%dT%H%M%SZ}"
    record: dict = {"started_at": started.isoformat(), "table": args.table}
    record["tenant_role"] = args.tenant_role_arn

    lf.grant_permissions(**grant)
    record["unfiltered_grant_at"] = datetime.now(UTC).isoformat()
    print(f"granted unfiltered SELECT on {args.table}; waiting {args.settle_seconds}s", flush=True)
    try:
        time.sleep(args.settle_seconds)
        broken = start_and_wait(sfn, args.check_arn, {}, name=f"drill-broken-{stamp}", poll_seconds=5)
        record["broken_run"] = {
            "execution": broken.execution_arn.rsplit(":", 1)[-1],
            "status": broken.status,
            "errors": _failed_states(sfn, broken.execution_arn),
        }
        print(f"broken run: {broken.status} {record['broken_run']['errors']}", flush=True)
    finally:
        lf.revoke_permissions(**grant)
        record["revoked_at"] = datetime.now(UTC).isoformat()
        print("revoked the unfiltered grant", flush=True)

    time.sleep(args.settle_seconds)
    restored = start_and_wait(sfn, args.check_arn, {}, name=f"drill-restored-{stamp}", poll_seconds=5)
    restored_name = restored.execution_arn.rsplit(":", 1)[-1]
    record["restored_run"] = {"execution": restored_name, "status": restored.status}
    print(f"restored run: {restored.status}", flush=True)

    time.sleep(60)  # SNS metrics are published per minute
    datapoints = cw.get_metric_statistics(
        Namespace="AWS/SNS",
        MetricName="NumberOfMessagesPublished",
        Dimensions=[{"Name": "TopicName", "Value": args.alerts_topic_arn.rsplit(":", 1)[-1]}],
        StartTime=started - timedelta(minutes=1),
        EndTime=datetime.now(UTC) + timedelta(minutes=1),
        Period=60,
        Statistics=["Sum"],
    )["Datapoints"]
    record["sns_messages_published_in_window"] = sum(d["Sum"] for d in datapoints)

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"tenant_access_drill_{stamp}.json"
    out.write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps(record, indent=2, default=str))
    ok = (
        record["broken_run"]["status"] == "FAILED"
        and "ForeignRowsVisible" in record["broken_run"]["errors"]
        and restored.succeeded
        and record["sns_messages_published_in_window"] >= 1
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
