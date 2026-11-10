# Runbook: pipeline alarms (dev/observability)

Every alarm notifies `procurelake-dev-pipeline-alerts` on ALARM and on OK. Start from the
dashboard `procurelake-dev-operations`.

| Alarm | Means | First step |
|---|---|---|
| `procurelake-dev-sfn-cdc-processing-failed` | A CDC run failed, timed out or was aborted | Step Functions execution history: which state failed. CDC apply: Glue run logs. A held lease clears itself after its TTL (ADR 0028) |
| `procurelake-dev-sfn-dbt-transformation-failed` | dbt failed, or the tenant access check after it failed | Container exit code and `dbt/runs/<run id>/run_results.json` in the artifacts bucket. If the access check failed, see below |
| `procurelake-dev-sfn-tenant-access-check-failed` | A tenant lost access to a mart, or can see another tenant's rows | `ForeignRowsVisible`: find the extra Lake Formation grant and revoke it (`list-permissions` on the table). Access lost: a mart was dropped (full refresh), so re-apply `dev/security` with `-parallelism=1` (ADR 0030) |
| `procurelake-dev-sfn-tenant-exports-failed` | A daily export failed | Execution history: Athena error in `Unload`, or S3 in `WriteManifest`. Rerun with `make tenant-exports-run` (each run writes a new `run=` prefix) |
| `procurelake-dev-cdc-source-to-silver-latency` | A CDC batch reached Silver more than 15 min after its oldest source commit | After a stall, expected once for the backlog (see `cdc-dms-stall.md`). Otherwise slow Glue starts or a long-held lease |
| `procurelake-dev-cdc-rejected` / `-held` | CDC events were quarantined, or keys are held at an older version | Quarantine prefix `quarantine/silver_cdc/<table>/batch_id=…/` lists each event with its reasons. Fix the source row; the next change supersedes the held version |
| `procurelake-dev-cdc-blocked` | A batch exceeded the rejection ratio and was not applied | As above. The batch is retried on every run until the files are fixed or the gate is changed (ADR 0028) |
| `procurelake-dev-cdc-dms-source-latency` (dev/cdc) | DMS stopped or is not capturing | `cdc-dms-stall.md` |
