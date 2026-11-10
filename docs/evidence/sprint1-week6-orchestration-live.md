# Evidence: Week 6, orchestration deployed and the dbt state machine run live

**Scope:**
- `procurelake-dev-dbt-transformation`, run live: one success and one deliberate failure.
- `procurelake-dev-cdc-processing` and its 5-minute schedule are deployed. Running them needs the
  CDC stack and is not covered here.

| | |
|---|---|
| Date | 2026-11-10 |
| Image | `procurelake-dev-dbt-runner@sha256:6bec6d0f758f…b447a7` (ARM64, Debian 13, Python 3.11.15, dbt-core 1.12.6, dbt-athena 1.11.1) |
| ECR scan of that image (basic) | 0 critical, 2 high (glibc), 9 medium, 3 low. The previous bookworm image had 10 critical and 57 high and was replaced before any task ran |

## Success: `manual-20261110T161428Z`

| Check | Result |
|---|---|
| Execution | SUCCEEDED, 11:14:30 → 11:16:20 local (110 s, including Fargate start and image pull) |
| Task | Fargate on arm64. The image was pulled by the pinned digest from the OCI image index; the container exited 0 |
| dbt (container log) | `Done. PASS=59 WARN=0 ERROR=0 SKIP=0`: 10 models and 49 tests, 48.0 s, running as the data-engineer role |
| Artifacts | `s3://<artifacts>/dbt/runs/manual-20261110T161428Z/run_results.json` and `manifest.json` |

## Deliberate failure: `drill-exit-…`

The input was `run_id = "drill/not-a-valid-run-id"`. Step Functions accepts it, but the
entrypoint's run-ID check rejects it, so dbt ran and then the artifact upload raised. The
container exited 1.

| Check | Result |
|---|---|
| Execution | **FAILED**, with `States.TaskFailed` |
| Task | `ExitCode: 1`, `StopCode: EssentialContainerExited` |
| Retries | none: one `TaskFailed` event. dbt failures are not retried; only ECS capacity/API errors are |

This answers what the AWS docs leave open: `ecs:runTask.sync` fails the state when the
container exits non-zero. The explicit exit-code check is kept as a second safeguard.

## Not yet shown

- `cdc_processing` end to end: overlap skip, CDC apply with retry, nested dbt, SNS alert.
- The 5-minute schedule.
- Freshness under scheduling.
- A live CDC retry.

All of these need a CDC session.
