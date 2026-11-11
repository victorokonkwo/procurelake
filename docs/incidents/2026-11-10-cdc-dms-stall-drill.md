# Incident report: CDC stalled silently (DMS task stopped), controlled drill

| | |
|---|---|
| Date | 2026-11-10 |
| Environment | dev (synthetic tenants and data) |
| Severity | SEV-2: freshness SLO missed for the affected changes, no data loss |
| Status | Resolved |
| Drill or real | Drill (controlled), `make cdc-stall-drill`, the Week 8 completion gate |

Template: `observability/incident_templates/incident_report.md`. Runbook:
`docs/runbooks/cdc-dms-stall.md`. Raw record: `build/evidence/cdc_stall_drill_20261110T233820Z.json`
(not committed).

## Summary
The AWS DMS replication task was stopped on purpose, and 25 source changes were committed while
it was down. The scheduled pipeline did not notice: both `cdc_processing` runs during the stall
SUCCEEDED, because there was nothing new to apply. Monitoring did notice, twice:
- The EventBridge task-state rule sent "task stopped" within the same minute.
- The DMS latency alarm, which treats missing data as breaching, went to ALARM 11.8 minutes
  after the stop.

DMS was resumed from its own checkpoint (`resume-processing`). Scheduled runs alone brought all
25 changes into Silver and Gold 5.8 minutes later, with no loss and no duplicates.

## Timeline (UTC)
| Time | Event | Evidence |
|---|---|---|
| 23:38:20 | Drill starts. DMS task running, latency alarm OK | drill record |
| 23:38:40 | **Failure:** `StopReplicationTask`; task `stopped` | drill record |
| 23:38:40 | 25 changes committed on the source during the stall | `clock_timestamp()` of the source transactions |
| 23:38 | **First alert:** EventBridge rule `procurelake-dev-cdc-dms-task-state` invoked (task state change), SNS message published | `AWS/Events` Invocations 1 in the 23:35–23:40 bucket, FailedInvocations 0; SNS published 1 at 23:38 |
| 23:39 | Last `CDCLatencySource` datapoint (0 s); DMS publishes nothing after the stop | `AWS/DMS` datapoints |
| 23:40–23:50 | Two scheduled `cdc_processing` runs SUCCEED with nothing new to apply | drill record (`SUCCEEDED: 2, FAILED: 0`) |
| 23:50:18 | **Alarm:** `procurelake-dev-cdc-dms-source-latency` OK → ALARM; 24 changed keys still stale in Silver | alarm history, drill record |
| 23:51:19 | **Recovery:** `StartReplicationTask --start-replication-task-type resume-processing`; task running | drill record; EventBridge invoked again (23:50–23:55 bucket) |
| 23:52:18 | Latency alarm ALARM → OK (DMS metrics flowing again) | alarm history |
| 23:55:17 | `procurelake-dev-cdc-source-to-silver-latency` OK → ALARM: the backlog batch's oldest change reached Silver 921.7 s after its commit | alarm history; `SourceToSilverSeconds` max 921.746 |
| 23:57:08 | **Restored:** every stall change correct in Silver (tombstones for deletes) and in Gold | `check_layers`: 0 mismatches |
| 00:10:17 (11-11) | Freshness alarm ALARM → OK by itself; no CDC batch since 23:54 | alarm history; `SourceToSilverSeconds` datapoints |

## Impact
- **Stale window:** the last stalled commit at 23:38:40 was correct in Gold at 23:57:08, so
  **1,108 s (18.5 min)**.
- **Changes affected:** 25 row changes. At detection, `check_layers` (latest change per key)
  reported 24 keys stale in Silver.
- **Lost changes: 0.** After recovery, every changed key's latest after-image is in Silver and
  in Gold; deletes are tombstones in Silver and absent from Gold.
- **Duplicated changes: 0.** One row per primary key in `silver_invoices` (2,877 rows, 2,877
  keys), `silver_purchase_orders` (1,481), `silver_awards` (628) and `silver_suppliers` (105).
  The two `dbt_transformation` runs in the window passed, including Gold's uniqueness tests, and
  so did their tenant access checks.
- **SLO impact:** the freshness target (95% of changes in Gold within 15 minutes) was missed for
  these 25 changes (18.5 min). Measured once, on this drill.

## Detection
- **The pipeline could not report it.** No execution failed during the stall: `cdc_processing`
  succeeded twice, and `dbt_transformation` and `tenant_access_check` had no failures.
- **EventBridge reported it within the minute.** It is the fastest signal, but AWS delivers DMS
  events on a best-effort basis.
- **The latency alarm took 706 s (11.8 min)** from the stop, about 11.5 min after the last
  datapoint. It is configured as 5 of 5 one-minute periods with missing data breaching, so a
  quicker transition was expected. The extra delay was measured, not explained. CloudWatch's
  handling of missing data for a metric that stops entirely is the likely factor; see the
  actions.

## Root cause
Deliberate: an operator stopped the replication task. In a real incident the same symptoms
would come from a stopped or failed task, an endpoint or credential problem, or the source being
down. The runbook covers each.

## Recovery
The runbook was followed. DMS was resumed from its own checkpoint (never a fresh full load,
which would start a new `load_id`; ADR 0028). Nothing was applied by hand: the 5-minute schedule
applied the backlog, and the DynamoDB checkpoint and lease prevented double application.
Correctness was checked per key in Silver and Gold, and for duplicates in Silver.

## What went well / what did not
- **Went well:** the silent failure was detected by two independent signals, and recovery
  needed one command. The backlog batch was correctly flagged by the freshness alarm (921.7 s
  against 900 s), so the SLO miss was visible without a manual check.
- **Did not go well:**
  - The latency alarm was slower than designed (11.8 min).
  - On stack creation, the same alarm went to ALARM and sent an email before the new task
    published any metric. The reason is that missing data breaches. It cleared by itself at
    23:37 but is noise.
  - The freshness alarm's OK carries no information: it cleared by itself at 00:10:17Z,
    15 minutes after it fired, with no newer batch (its last datapoint was at 23:54Z).

## Actions
| Action | Owner | Status |
|---|---|---|
| Measure the latency alarm's missing-data behaviour, or alarm also on the EventBridge "task stopped" event (it was the fastest signal) | data platform | Open (open question 39) |
| Avoid the creation-time alarm: suppress it until the task has published metrics, or accept and document it | data platform | Open (open question 39) |
| Decide whether the freshness alarm's OK should require an on-time batch (today it clears by itself about 15 minutes after a late one) | data platform | Open (open question 39) |
| Repeat the drill with a larger stall (minutes of backlog at higher volume) | data platform | Open |

## Evidence
- **Drill record:** `build/evidence/cdc_stall_drill_20261110T233820Z.json`.
- **Alarm histories:** `procurelake-dev-cdc-dms-source-latency` and
  `procurelake-dev-cdc-source-to-silver-latency`.
- **Metrics:** `AWS/Events` Invocations for rule `procurelake-dev-cdc-dms-task-state`;
  `AWS/SNS` NumberOfMessagesPublished, 6 in the window (latency OK from creation at 23:37, task
  stopped, latency ALARM, task resume event, latency OK, freshness ALARM).
- **DMS task:** `arn:aws:dms:ca-central-1:697191766499:task:VZEL6R3QVZEATBBURJDW2WFKDA`.
