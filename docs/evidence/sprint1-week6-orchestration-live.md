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

## CDC session with the schedule running (2026-11-10, 16:58–17:25 UTC)

The deferral fix (commit `04b19df`, deployed before the schedule existed) defers a table's
current load until DMS reports its full load complete. It went live first. DMS had already
finished its full load (all 8 tables `Table completed`) when the first scheduled run started.

### Scheduled executions of `procurelake-dev-cdc-processing`

| Execution (start, local) | Status | Steps | Notes |
|---|---|---|---|
| `206af34d` 11:59:00 | SUCCEEDED 5 m 4 s | count → ApplyCdc (Glue 147 s, DMS task ARN passed) → TransformGold | Applied this session's full load as a complete load |
| `206af34e` 12:03:29 | SUCCEEDED 0.2 s | count only | **Overlap skip**: `206af34d` was still running |
| `206af34f` 12:08:29 | SUCCEEDED | count → ApplyCdc → TransformGold | Carried the scheduled-freshness workload |
| `206af350` 12:13:29 | **FAILED** | count → ApplyCdc (2 attempts) → NotifyFailure | Failure drill, below |
| `206af351` 12:18:29 | SUCCEEDED 6 m 12 s | count → ApplyCdc → TransformGold | **Recovery**, below |
| `206af353` 12:23:29 | SUCCEEDED 0.2 s | count only | Overlap skip (`206af351` running) |

After the full load, Silver `silver_invoices` had 2,823 active rows, exactly the re-seeded source.
The 43 invoices inserted in earlier sessions, which the re-seeded source does not have, became
tombstones. Gold `fct_invoice` had 2,823 rows.

### Scheduled freshness (`make cdc-freshness-scheduled`, seed 31)

The harness only applied the workload. The schedule ran the pipeline, and the harness polled
Silver and Gold every 30 s until every change was visible.

| Layer | Changes | min | p50 | p95 | max | Gate (p95) |
|---|---|---|---|---|---|---|
| Silver | 42 | 335.1 s | 336.7 s | **338.5 s** | 338.6 s | 900 s: met |
| Gold | 42 | 456.0 s | 457.6 s | **459.4 s** | 459.6 s | 900 s: met |

There were 0 mismatches. These latencies include waiting for the next 5-minute tick, plus up to
30 s of polling resolution. That is why they are higher than the hand-started runs in Week 5
(140 s and 190 s).

### Failure and recovery drill (`make cdc-failure-drill`)

The drill held the `silver_invoices` lease as another writer would (the real `CheckpointStore`),
from 17:13:15 to 17:20:15 UTC, and applied 17 row changes.

| Step | Observed |
|---|---|
| Failed run `206af350` | Glue attempt 1 failed at 12:16:26 local with `CdcJobError: invoices: silver_invoices: another writer holds the lease` (other tables applied). The **retry** started at 12:17:26, 60 s later, and failed the same way at 12:18:28. **SNS publish** at 12:18:28, then `Fail` |
| Alert | CloudWatch `AWS/SNS NumberOfMessagesPublished` = 1 in the window (no email is subscribed) |
| Recovery run `206af351` | Glue `jr_95820e8…` ran 12:19:29–12:22:02 and reached `silver_invoices` after the lease expired. The invoices checkpoint moved from `20261110T171003Z-cbf63c83` to `20261110T172047Z-b1748d42` (13 events), then dbt ran. SUCCEEDED at 12:24:41 |
| End state | 0 leases left. Every drill change is in Silver and Gold (0 mismatches) |

This is the live retry check that C5 was missing. The retried run failed safely, without a
partial checkpoint, and the next run replayed the pending files with no duplicates and no
missing changes.

**Drill script bug found and fixed:** the script took the first successful execution after the
lease expired as the recovery. That was the 0.2 s overlap skip `206af353`. It now requires an
execution that ran `ApplyCdc`, starting after the failed one. The table above names the real
recovery run.

## Still open

- **Volume.** These are runs of tens of changes. The 1,000–10,000-change test in the
  performance plan has not been run.
- **Alert delivery.** It is proven to SNS, but no subscriber is configured (`alert_email` unset).
