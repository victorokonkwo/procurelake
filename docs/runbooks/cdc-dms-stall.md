# Runbook: CDC stalled (DMS task stopped or not capturing)

**Alerts:**
- `procurelake-dev-cdc-dms-source-latency` (CloudWatch alarm on `AWS/DMS` `CDCLatencySource`).
- "ProcureLake dev CDC: DMS task event …" (EventBridge, replication task state change).

**Why it matters:** the failure is silent to the pipeline. `cdc_processing` keeps succeeding
every 5 minutes with nothing to apply, while Silver and Gold go stale and the freshness SLO
(95% of changes in Gold within 15 minutes) is missed. No data is lost while the replication slot
holds the WAL: DMS resumes from its own checkpoint.

## 1. Confirm

```bash
aws dms describe-replication-tasks --filters Name=replication-task-arn,Values=<task arn> \
  --query 'ReplicationTasks[0].[Status,StopReason,LastFailureMessage]'
```

- **`stopped`** with a `StopReason`: someone or something stopped the task. Check CloudTrail
  `StopReplicationTask`.
- **`failed`**: read `LastFailureMessage`, and the task log in CloudWatch Logs
  (`dms-tasks-<instance>`).
- **`running` but latency high**: the source connection or the replication slot. On the source:
  ```sql
  SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
  FROM pg_replication_slots;
  ```

On the operations dashboard (`procurelake-dev-operations`), "DMS: CDC latency" is empty or
rising while "CDC: events applied" is flat.

## 2. Recover

1. Fix the cause: the endpoint, credentials, the security group, or the source being up.
2. Resume from DMS's checkpoint. **Do not** restart with a fresh full load: that creates a new
   `load_id` and re-tombstones keys (ADR 0028).
   ```bash
   aws dms start-replication-task --replication-task-arn <task arn> \
     --start-replication-task-type resume-processing
   ```
3. Leave the schedule alone. The next `cdc_processing` runs apply the backlog in order; the
   DynamoDB checkpoint and the lease prevent double application.

## 3. Verify

- The task is `running` and `CDCLatencySource` is back under 5 minutes; the alarm returns to OK
  and sends an OK notification.
- `cdc_processing` executions succeed, and their CDC apply logs show `files>0` for the stalled
  tables.
- Changes committed during the stall are in Silver and Gold. `make cdc-freshness` measures new
  changes; for the stall window, check the keys with `procurelake.cdc.verify.check_layers` (as
  `make cdc-stall-drill` does).
- `procurelake-dev-cdc-source-to-silver-latency` may fire once for the backlog batch: that batch
  really did miss the 15-minute target. Record it in the incident report.

## 4. Report

Write an incident report from `observability/incident_templates/incident_report.md`: detection
time, stale window, changes affected, and whether any were lost or duplicated.
