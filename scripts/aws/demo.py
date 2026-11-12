"""End-to-end demo, one step at a time (implementation plan section 4; `make demo STEP=…`).

Follows one ``tenant_alder`` award through the platform. Every step reads live dev; only
``update`` writes, and only to the disposable RDS source (synthetic data).

  show     1. the award in PostgreSQL (picked once, remembered in build/demo/award.json)
  update   2. change its amount (AMOUNT=…); records the commit time
  bronze   3. the DMS change files in Bronze that carry the award, and its events in them
  silver   4. the award in Silver Iceberg, with its CDC lineage, and the Glue run that applied it
  gold     5. the award in Gold (fact and mart) as the data engineer
  tenants  6. the award's mart group as tenant_alder (visible) and tenant_birch (no rows)
  health   7. alarm states, the latest pipeline executions and the dashboard link
Step 8 (Bedrock) uses `make ai-review` / `make ai-decide` (ADR 0032).

The 5-minute ``cdc_processing`` schedule runs the CDC apply and dbt between steps 2 and 4.
Run ``make cdc-apply`` and ``make dbt-build`` by hand only when no scheduled run is active.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
from psycopg.rows import dict_row

from procurelake.common.athena import run_query
from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.operations import demo

STATE = Path(__file__).resolve().parents[2] / "build" / "demo" / "award.json"


def _load() -> dict:
    if not STATE.exists():
        raise SystemExit("run `make demo STEP=show` first")
    return json.loads(STATE.read_text())


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, default=str) + "\n")


def _print_rows(title: str, columns: list[str], rows: list) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (no rows)")
    for row in rows:
        print("  " + ", ".join(f"{c}={v}" for c, v in zip(columns, row, strict=True)))


def _pg(args, session):
    secret = json.loads(
        session.client("secretsmanager").get_secret_value(SecretId=args.master_secret_arn)["SecretString"]
    )
    return psycopg.connect(
        host=args.host, port=args.port, dbname=args.database, user=secret["username"],
        password=secret["password"], sslmode="require", connect_timeout=15, row_factory=dict_row,
    )  # fmt: skip


def step_show(args, session) -> None:
    award_id = demo.checked_uuid(args.award_id) if args.award_id else None
    with _pg(args, session) as conn:
        row = conn.execute(demo.PICK_AWARD_SQL, {"tenant": "tenant_alder", "award_id": award_id}).fetchone()
    if row is None:
        raise SystemExit("no active tenant_alder award found")
    _print_rows("PostgreSQL procurement.awards", list(row), [list(row.values())])
    _save({k: str(v) for k, v in row.items()})


def step_update(args, session) -> None:
    state = _load()
    amount = demo.checked_amount(args.amount)
    with _pg(args, session) as conn:
        row = conn.execute(
            demo.UPDATE_AWARD_SQL,
            {"amount": amount, "award_id": state["award_id"], "tenant": state["tenant_id"]},
        ).fetchone()
    print(f"award {row['award_id']}: {state['award_amount']} -> {row['award_amount']} {row['currency_code']}")
    print(f"committed at {row['committed_at'].isoformat()}")
    state.update(previous_amount=state["award_amount"], award_amount=str(row["award_amount"]),
                 committed_at=row["committed_at"].isoformat())  # fmt: skip
    _save(state)


def step_bronze(args, session) -> None:
    state = _load()
    s3 = session.client("s3")
    prefix = f"{args.cdc_prefix}/procurement/awards/"
    since = datetime.fromisoformat(state.get("committed_at", "1970-01-01T00:00:00+00:00"))
    objects = [
        o
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=args.raw_bucket, Prefix=prefix)
        for o in page.get("Contents", [])
        if not o["Key"].rsplit("/", 1)[-1].startswith("LOAD") and o["LastModified"] >= since
    ]
    print(f"s3://{args.raw_bucket}/{prefix}: {len(objects)} CDC file(s) written since the commit")
    for o in sorted(objects, key=lambda o: o["LastModified"]):
        body = s3.get_object(Bucket=args.raw_bucket, Key=o["Key"])["Body"].read()
        events = demo.award_events(pq.read_table(io.BytesIO(body)).to_pylist(), state["award_id"])
        mark = f"  <- {len(events)} event(s) for this award" if events else ""
        print(f"  {o['LastModified'].isoformat()}  {o['Size']:>7} B  {o['Key'].rsplit('/', 1)[-1]}{mark}")
        for e in events:
            print(
                f"      Op={e['op']} commit={e['commit_ts']} "
                f"award_amount={e['award_amount']} status={e['status']}"
            )


def _engineer(args):
    return build_session(region=args.region, role_arn=args.engineer_role_arn, session_name="demo").client(
        "athena"
    )


def step_silver(args, session) -> None:
    state = _load()
    r = run_query(
        _engineer(args),
        demo.silver_award_sql(args.silver_db, state["award_id"]),
        args.workgroup,
        args.silver_db,
    )
    _print_rows("Silver silver_awards (Iceberg)", r.columns, r.rows)
    runs = session.client("glue").get_job_runs(JobName=args.glue_job, MaxResults=3)["JobRuns"]
    print("\nLatest CDC apply runs:")
    for run in runs:
        started, seconds = run["StartedOn"].isoformat(), run.get("ExecutionTime", 0)
        print(f"  {run['Id'][:14]}…  {run['JobRunState']:<10} started {started}  {seconds} s")


def step_gold(args, _session) -> None:
    state = _load()
    athena = _engineer(args)
    r = run_query(athena, demo.gold_fact_sql(args.gold_db, state["award_id"]), args.workgroup, args.gold_db)
    _print_rows("Gold fct_contract_award (data engineer)", r.columns, r.rows)
    r = run_query(athena, demo.gold_mart_sql(args.gold_db, state["agency_id"], state["supplier_id"]),
                  args.workgroup, args.gold_db)  # fmt: skip
    _print_rows(
        "Gold mart_awarded_value, the award's agency and supplier (awarded value, not spend)",
        r.columns,
        r.rows,
    )


def step_tenants(args, _session) -> None:
    state = _load()
    roles, workgroups = json.loads(args.tenant_roles), json.loads(args.tenant_workgroups)
    sql = demo.gold_mart_sql(args.gold_db, state["agency_id"], state["supplier_id"])
    for tenant in demo.TENANTS:
        athena = build_session(
            region=args.region, role_arn=roles[tenant], session_name=f"demo-{tenant}"
        ).client("athena")
        r = run_query(athena, sql, workgroups[tenant], args.gold_db)
        _print_rows(
            f"As {tenant} (own role, workgroup {workgroups[tenant]}, Lake Formation row filter)",
            r.columns,
            r.rows,
        )


def step_health(args, session) -> None:
    cw = session.client("cloudwatch")
    alarms = cw.describe_alarms(AlarmNamePrefix="procurelake-dev")["MetricAlarms"]
    print("Alarms:")
    for a in sorted(alarms, key=lambda a: a["AlarmName"]):
        print(f"  {a['StateValue']:<17} {a['AlarmName']}")
    sfn = session.client("stepfunctions")
    for name, arn in json.loads(args.state_machines).items():
        if not arn:
            continue
        ex = sfn.list_executions(stateMachineArn=arn, maxResults=1)["executions"]
        last = f"{ex[0]['status']} {ex[0]['startDate'].isoformat()}" if ex else "no executions"
        print(f"Latest {name}: {last}")
    print(f"\nDashboard: https://{args.region}.console.aws.amazon.com/cloudwatch/home?region={args.region}"
          f"#dashboards/dashboard/{args.dashboard}")  # fmt: skip
    print("Quality tests: the dbt build log of the latest run (`make dbt-build` prints PASS/WARN/ERROR).")


STEPS = {"show": step_show, "update": step_update, "bronze": step_bronze, "silver": step_silver,
         "gold": step_gold, "tenants": step_tenants, "health": step_health}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("step", choices=list(STEPS))
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--award-id")
    p.add_argument("--amount")
    for name in ("host", "database", "master-secret-arn", "raw-bucket", "cdc-prefix", "glue-job",
                 "engineer-role-arn", "workgroup", "silver-db", "gold-db", "tenant-roles",
                 "tenant-workgroups", "state-machines", "dashboard"):  # fmt: skip
        p.add_argument(f"--{name}", default="")
    p.add_argument("--port", type=int, default=5432)
    args = p.parse_args(argv)
    if args.step == "update" and not args.amount:
        p.error("update needs --amount (make demo STEP=update AMOUNT=…)")
    STEPS[args.step](args, build_session(region=args.region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
