# ADR 0040: Batch ingestion and daily data-quality state machines (task runner on Fargate)

- **Status:** Accepted (built and tested offline; the checks ran live from the operator
  machine; not yet deployed)
- **Date:** 2026-11-12
- **Implementation:**
  - Container: `jobs/containers/task_runner/` (Dockerfile, allow-listed entrypoint)
  - Checks: `src/procurelake/quality/` (`checks.py`, `cli.py`)
  - Spark-free spec loading: `src/procurelake/lakehouse/silver/resources.py`
  - State machines: `orchestration/step_functions/{batch_ingestion,data_quality}.asl.json`
  - Terraform:
    - `infrastructure/live/dev/data_platform/batch_and_quality.tf`
    - the `dbt_runner` module gains `container_name` and `ephemeral_storage_gib`
    - the IAM module gains `ingestion_service_principals`
    - observability alarms on both machines
  - Make: `task-runner-image`, `batch-ingestion-run`, `data-quality-run`, `data-quality-local`
  - Tests:
    - `tests/unit/quality/`, `tests/unit/orchestration/test_task_runner_entrypoint.py`;
    - Terraform tests in `data_platform`, `observability` and `modules/iam`.
- **Owner decisions (2026-11-12):**
  - batch ingestion runs as a Fargate task;
  - data quality runs as scheduled checks with alerts: dbt test, Bronze-to-Silver
    reconciliation, a report and SNS;
  - the document-intelligence machine waits for the Bedrock quota, so it is built and verified
    live together with extraction.

## Context
The plan's orchestration line lists six state machines. Four existed:
- `cdc_processing`, `dbt_transformation`, `tenant_exports`, `tenant_access_check`.

Batch ingestion only ran from the operator's laptop (`make ingest-*`). Data quality ran only
inside `dbt build`, once per CDC run, and nothing re-checked Bronze-to-Silver consistency after
the fact.

## Decision
1. **One task runner image**, separate from dbt:
   - Python 3.11, the same pinned base images as the dbt runner, and the project's runtime
     dependencies only (no dev, ai or dbt groups). It is about 478 MB.
   - The image bundles the Silver contracts and configs, through the wheel's `force-include`.
   - It runs the **dbt runner module a second time** (`procurelake-dev-task-runner`), with its
     own ECR repository (immutable tags, scan on push), cluster, log group and egress-only
     security group.
   - The task definition uses the image by digest, 1 vCPU and 2 GiB. The default 20 GiB of
     ephemeral storage holds the 655 MB Canada file.
2. **The entrypoint runs only three tasks:** `ingest-usaspending`, `ingest-canada-contracts`,
   `data-quality`.
   - Arguments are built in the entrypoint. Only the run id, a validated date and a bounded
     day count come from the environment.
   - Rights come from the task role. There is no `--role-arn` in the container.
3. **Roles:**
   - The task definition's role is the **ingestion role**. Its trust now allows
     `ecs-tasks.amazonaws.com`, limited by `aws:SourceAccount`. This reverses the earlier "the
     ingestion role is never assumed by a service" (IAM module comment, ADR 0029 era), because
     Fargate ingestion needs it.
   - Its permissions are unchanged, apart from ADR 0039's abort of its own uploads.
   - The data-quality machine overrides the task role with the **data engineer**
     (`Overrides.TaskRoleArn`) for the Silver checks. The ingestion role cannot read Silver,
     and the data engineer cannot write Bronze.
4. **`batch_ingestion`:**
   - input `{"task": …}`, checked by a Choice state;
   - `ecs:runTask.sync`, then an exit-code check (as in `dbt_transformation`);
   - an SNS alert and `Fail` on any failure;
   - only ECS capacity and API errors are retried. A failed ingestion is not retried in the
     execution: the connectors are gap-filling and create-only, so the next scheduled run
     covers the gap.
5. **Schedules** (EventBridge Scheduler, America/Toronto; an empty variable disables one):
   - USAspending, daily at 07:00: `--since 2026-10-23 --max-days 3`, settled days only
     (ADR 0020);
   - Canada contracts, Mondays at 07:30: `--skip-if-unchanged` sends one HEAD request and
     downloads only when the `ETag` or size differs from the last succeeded batch. The source
     is replaced in place quarterly (`P3M`);
   - data quality, daily at 08:00: after maintenance (03:30) and the tenant exports (06:00).
6. **`data_quality`:**
   - `dbt test` in the dbt runner (a test, not a rebuild). It includes the Gold-to-Silver
     reconciliation tests.
   - Then the Silver checks in the task runner. Both always run.
   - The execution succeeds only if both exit 0. Otherwise it sends an SNS alert and fails.
   - The Silver checks:
     - **Latest Silver run summary** (`pipeline_runs/silver/<source>/…`): every table's input
       equals its manifest's record count; input equals valid plus rejected; the quarantine
       holds exactly the rejected rows; no table is blocked.
     - **Current Silver tables** (Athena): no primary key appears twice, and no null
       `tenant_id`.
   - Every check, passed or not, is written to
     `pipeline_runs/quality/run_date=/run_id=/report.json`.
7. **Failure alarms:** observability adds both machines to its "failed, timed out or aborted"
   alarms once their ARNs exist.
8. **Gating.** Nothing is created until `task_runner_image_digest` is set (`make
   task-runner-image`, then the `TASK_RUNNER_IMAGE_DIGEST` repository variable for CI). The
   exception is the runner's repository and cluster, which the first push needs.

## Consequences
- **Measured from the operator machine** (`make data-quality-local`, 2026-11-12, as the data
  engineer on live dev): **35 checks, 0 failed**. That is 7 tables × (manifest, split,
  not blocked, duplicates, null tenant).
  - The latest summary is batch `20261110T093202Z-983c92e5`. Silver currently holds 628 awards,
    5,561 invoices and 1,481 purchase orders, with no duplicate keys.
  - The deployed machines have not run yet. That needs, in order: the PR, the image push, the
    CI apply after owner approval, and then `make data-quality-run` and `make
    batch-ingestion-run`.
- **Cost:** two or three short Fargate tasks a day (1 vCPU, 2 GiB, minutes each), one small
  Athena query per Silver table a day, and a 655 MB snapshot only when Canada publishes a new
  file.
- **The summary check covers the latest snapshot run.** CDC changes are reconciled by the CDC
  apply's own checks and the freshness harness, not here.
- **Destroy-dev:** the runner's repository and log group are kept by the existing patterns
  (ADR 0038). Its machines, schedules and roles are destroyed with `data_platform`.

## Alternatives considered
- **Lambda for ingestion:** rejected by the owner. The 15-minute and 10 GB `/tmp` limits sit at
  the edge for the Canada file, and dependencies would need separate packaging.
- **Reconciliation in dbt only:** dbt cannot read manifests or quarantine files. The
  Gold-to-Silver checks stay in dbt, and the Bronze-to-Silver checks need Python.
- **The ingestion role for the Silver checks:** rejected. It would need Silver read rights it
  has no other use for.
