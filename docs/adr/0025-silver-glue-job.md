# ADR 0025: Silver Glue job (packaging, deployment, permissions)

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `jobs/glue/bronze_to_silver.py`, `src/procurelake/lakehouse/silver/job.py`,
  `infrastructure/modules/glue_spark_job/`, `infrastructure/live/dev/data_platform/`

## Context
ADR 0024 defined the Silver framework and tested it locally. Week 3 also needs it running on AWS
Glue 5.1 against dev Bronze, with the Silver tables queryable in Athena.

## Decision
1. **Thin entrypoint.** `jobs/glue/bronze_to_silver.py` only calls `procurelake.lakehouse.silver.job.main`.
   It never calls `sys.exit`: Glue marks a run FAILED on any `SystemExit`, including
   `SystemExit: 0`. We saw this on the first dev run. A unit test guards against it.
2. **One run = one snapshot batch for every table.** The job applies the requested batch, or
   by default the latest batch with a succeeded manifest for all seven tables. Each table runs
   independently. A run summary is written to
   `artifacts/pipeline_runs/silver/postgres/batch_id=<id>/run_id=<glue run id>/summary.json`.
   If the quality gate blocks any table, the job then raises.
3. **Rules ship with the code.** The wheel bundles `contracts/source/` and
   `configs/pipelines/silver/` under `procurelake/_bundled/` (hatch `force-include`). An editable
   checkout reads them from the repository. Wheel builds are reproducible: two builds had the same SHA-256.
4. **Dependencies are exact.**
   - `--additional-python-modules` installs the wheel plus the pins in
     `jobs/glue/silver_python_modules.txt` (the pydantic and PyYAML closure). It uses
     `--python-modules-installer-option --no-deps`.
   - boto3 and botocore come from the Glue runtime. psycopg and requests are not installed.
   - Tests check the pins against `uv.lock`, check the closure is complete, and check that the
     job imports without psycopg or requests.
5. **Content-addressed code.** The wheel is uploaded to `glue/wheels/<sha256>/…`, and the job's
   `--wheel-sha256` argument records which build ran. Terraform uploads the script and wheel.
   It holds no transformation logic.
6. **Iceberg configuration** follows the AWS Glue Iceberg documentation for Glue 5.1 (Iceberg 1.10.0):
   - `--datalake-formats iceberg`
   - a Spark catalog `glue_catalog` with `org.apache.iceberg.aws.glue.GlueCatalog` and `S3FileIO`
   - the session time zone set to UTC
   - Silver tables at `s3://<lake>/silver/<table>`, in the Glue database
     `procurelake_<env>_silver` (created in foundation)
7. **Run controls:**
   - One concurrent run, so there is one writer per table.
   - No automatic retries.
   - Bookmarks off, because the job tracks applied batches in table properties (ADR 0024).
   - A 30-minute timeout.
   - G.1X with 2 workers.
   - `--TempDir` in the artifacts bucket, so Glue does not create its default temporary bucket.
8. **Job role** (`procurelake-<env>-bronze-to-silver`):
   - Trusted only by `glue.amazonaws.com` with `aws:SourceAccount` set to this account.
   - **Reads:** `bronze/postgres/*` and the postgres manifests.
   - **Writes:** `silver/*` in lake, `quarantine/silver/*` in raw, and the Silver run summaries.
   - **Catalog:** Get/Create/Update on the Silver database's tables only.
   - **Own resources:** its own code objects, temp prefix and log groups.
   - **Denied:** deletes under `bronze/*` and `documents/*`. Quarantine allows deletes because
     Spark's file committer renames its output there.
9. **Stacks.** The job lives in a new root, `live/dev/data_platform`, which reads foundation
   outputs through `terraform_remote_state`. The Glue log groups `/aws-glue/jobs/{error,output}`
   are managed there with 30-day retention.

## Consequences
- **Live-run evidence:** `docs/evidence/sprint1-week3-silver-live.md`.
- **Cost:** the measured run cost is in the evidence. The pinned modules are downloaded from PyPI
  each time a run starts. Hosting dependency wheels in S3 is open question 32.
- **Glue 6.0** is listed by the AWS Pricing API at $0.308/DPU-hour in ca-central-1, against
  $0.44 for 5.1. Moving would change the runtime versions in ADR 0015 (open question 31).
- **Log encryption:** the log groups use CloudWatch default encryption, not the platform KMS key
  (open question 33).
- **Not covered:** USAspending is not built into Silver yet. This job covers the operational
  (PostgreSQL) tables only.
