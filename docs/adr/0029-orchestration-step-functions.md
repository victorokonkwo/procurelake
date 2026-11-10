# ADR 0029: Orchestration with Step Functions, EventBridge Scheduler and an ECS Fargate dbt runner

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:**
  - State machines: `orchestration/step_functions/{cdc_processing,dbt_transformation}.asl.json`
  - Container: `jobs/containers/dbt_runner/`
  - Terraform: `infrastructure/modules/{public_network,dbt_runner}`, `live/dev/data_platform` (machines, roles, SNS) and `live/dev/cdc` (schedule)

## Context
Week 6 replaces the manual "CDC apply, then `make dbt-build`" with orchestration (ADR 0006). The
owner decided on 2026-11-10:
- dbt runs as an **ECS Fargate** task (open question 16)
- `cdc_processing` runs **every 5 minutes, only while the CDC stack is up**
- this week covers CDC processing and dbt transformation; the other state machines come in their own weeks

## Decision
1. **dbt runner image.**
   - The base is `python:3.11.15-slim-trixie`, pinned by digest, with Debian security updates
     applied at build time. Python dependencies are the locked `dbt` group only. The process runs
     as non-root (UID 10001) and writes only under `/tmp`.
   - The entrypoint runs one allow-listed dbt command, uploads `run_results.json` and
     `manifest.json` to `artifacts/dbt/runs/<run id>/`, and exits with dbt's exit code.
   - ECR: immutable tags, scan on push, KMS-encrypted, the last 10 images kept. The task definition
     runs the image **by digest** (`make dbt-image` records it).
2. **Identity.** The task role is the **data-engineer role**, the same identity as
   `make dbt-build`. Only that role trusts `ecs-tasks.amazonaws.com` (with `aws:SourceAccount`);
   the ingestion role does not. A test checks both.
3. **Network.** A VPC with two public subnets and no NAT gateway. The task gets a public IP,
   which AWS lists as one way for a Fargate task to reach ECR and AWS APIs. The security group
   allows no ingress and HTTPS egress only.
4. **`dbt_transformation`.**
   - `ecs:runTask.sync`, retried only on ECS capacity or API errors (`ECS.AmazonECSException`,
     `AmazonECS.Unknown`). A dbt failure is never retried.
   - An explicit `ExitCode == 0` check follows, because the docs do not say how a non-zero exit is
     treated. **Observed live:** a non-zero exit fails the task state with `States.TaskFailed`
     (`ExitCode: 1`, `StopCode: EssentialContainerExited`). The check stays as a second safeguard.
5. **`cdc_processing`.**
   1. Count its own running executions (`aws-sdk:sfn:listExecutions`) and succeed immediately if
      another is running. A 5-minute schedule can overlap a slow run, and Glue allows one run.
   2. Run the Glue CDC apply (`glue:startJobRun.sync`), retried once (safe because it is
      idempotent, ADR 0028).
   3. Run `dbt_transformation` as a nested execution (`startExecution.sync:2`).
   4. Any error publishes to the SNS topic `procurelake-dev-pipeline-alerts` (KMS-encrypted) and
      ends in `Fail`.
6. **Schedule.** EventBridge Scheduler `rate(5 minutes)`, with the flexible window off. It is
   defined in `dev/cdc` and exists only while DMS does, so nothing runs when CDC is down. The
   scheduler role may start only `cdc_processing`, and its trust uses `aws:SourceAccount` and
   the schedule group's `aws:SourceArn`.
7. **IAM** follows the Step Functions docs' `.sync` policies: the events rules
   `StepFunctionsGetEventsFor{ECSTask,StepFunctionsExecution}Rule`, with `iam:PassRole` limited to
   ECS tasks.

## Consequences
- **Evidence:** `docs/evidence/sprint1-week6-orchestration-live.md`.
- **Image vulnerabilities:**
  - The first image, on Debian bookworm (oldstable), had 10 critical and 57 high findings in
    the ECR basic scan, all in OS packages.
  - The current image has 0 critical and 2 high (glibc, no Debian fix at build time). Rebuild
    when Debian ships the fix.
  - ECR shows no scan status on the OCI image index; read the ARM64 image manifest's findings.
- **Cost:**
  - Idle: about $0.07/month for ECR storage. The VPC, cluster and state machines cost nothing
    idle. A dbt task (0.5 vCPU, 1 GiB, about 2 minutes) costs well under a cent.
  - While CDC is up, the scheduled Glue runs (about $0.36/hour) dominate.
- **Alerts:** no email is subscribed to the SNS topic unless `alert_email` is set (git-ignored
  tfvars).
- **Still to do for the Week 6 gate:** a CDC session with the schedule running. Measure
  scheduled freshness, and drill a failure and a retry through `cdc_processing` (this also
  covers the live-retry part of C5).
