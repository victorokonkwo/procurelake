# Runbook: Iceberg maintenance failed

**Alert:** "ProcureLake dev: Iceberg maintenance …" (EventBridge rule
`procurelake-dev-iceberg-maintenance-failed`: Glue job `procurelake-dev-iceberg-maintenance` went
`FAILED`, `TIMEOUT` or `ERROR`), sent to `procurelake-dev-pipeline-alerts`.

**Why it matters:** nothing breaks right away. Readers still get correct rows, but every missed
day leaves more small files and delete files for each query to read, and more snapshots and
unreferenced files in S3. It is urgent only if it keeps failing for several days (ADR 0033).

## 1. Find which tables failed

The job attempts every table, writes a JSON report, then fails if any table failed. The report
is in the artifacts bucket:

```bash
aws s3 ls s3://procurelake-dev-artifacts-<account>-ca-central-1/iceberg_maintenance/run_date=<YYYY-MM-DD>/
aws s3 cp s3://…/iceberg_maintenance/run_date=<date>/<job run id>.json - | jq '.failed, (.tables[] | select(.status=="failed") | {table, error})'
```

No report means the job failed before writing one (arguments, IAM, the wheel). Read the Glue
run's error and the `/aws-glue/jobs/error` log group:

```bash
aws glue get-job-run --job-name procurelake-dev-iceberg-maintenance --run-id <id> \
  --query 'JobRun.[JobRunState,ErrorMessage]'
```

## 2. Common causes

| `error` in the report | Cause | Action |
|---|---|---|
| `ICEBERG_COMMIT_ERROR` (after 3 attempts) | A writer kept committing to the table: dbt on a Gold mart, or a Glue job on Silver | Rerun once the writer is idle |
| `live records changed during maintenance` | Rows changed between the before and after reads. A writer ran (dbt runs on Gold marts are not leased) | Check that the dbt run succeeded, then rerun. The table's data is the writer's, not lost |
| `AccessDeniedException` / `ACCESS_DENIED` | IAM or Lake Formation on that table changed | Compare `local.maintenance_policy` in `infrastructure/live/dev/data_platform/main.tf` with the table's location and database |
| `TABLE_NOT_FOUND` | A table in `maintenance_*_tables` was dropped or renamed | Fix the variable and plan/apply data_platform |

A table listed under `skipped` is **not** a failure: the CDC apply job held its DynamoDB lease,
and the next run maintains it. If the same table is skipped every day, check for a lease that
was never released:

```bash
aws dynamodb get-item --table-name procurelake-dev-cdc-checkpoints \
  --key '{"pk":{"S":"<silver table>"},"sk":{"S":"lease"}}'
```

Once `expires_at` (epoch seconds) has passed, the next writer takes the lease over, so a stale
lease clears itself; maintenance asks for 30 minutes. Do not delete a lease while a CDC run is in
progress.

## 3. Rerun

```bash
make iceberg-maintenance-run
```

It is safe to rerun at any time: retention is re-applied, OPTIMIZE on a compact table changes
nothing, and VACUUM never removes a snapshot younger than the retention.

## 4. Verify

- The new report has `"failed": []`.
- For each Gold mart, `after.data_files` is small (1 in dev today), `after.delete_files` is 0,
  and `before.live_records == after.live_records`.
- `make test-aws` passes. It checks tenant isolation on the rewritten marts.

## Notes

- `SHOW TBLPROPERTIES` in Athena did not list `vacuum_max_snapshot_age_seconds` on the
  Spark-created Silver tables, although the property was written. Check
  `history.expire.max-snapshot-age-ms` in the table's current `metadata.json` (the Glue table's
  `metadata_location`) instead.
- Expired snapshots can no longer be used for time travel: Silver keeps 7 days (at least 10
  snapshots), Gold marts 1 day (at least 5). Change retention through the Terraform variables,
  not by hand: the next run overwrites manual `ALTER TABLE` properties.
