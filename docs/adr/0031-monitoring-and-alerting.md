# ADR 0031: Monitoring and alerting: CDC metrics, CloudWatch alarms and EventBridge to SNS

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:**
  - Metrics: `src/procurelake/cdc/metrics.py` (published by `procurelake-dev-apply-cdc`)
  - Terraform:
    - `infrastructure/live/dev/observability`: state machine and CDC alarms, the dashboard
    - `infrastructure/live/dev/cdc`: the DMS alarm and the task-state rule
    - `modules/kms`: the key policy for SNS publishers
    - `live/dev/data_platform`: the alerts topic policy
  - Drill: `scripts/aws/cdc_stall_drill.py` (`make cdc-stall-drill`)
  - Runbooks: `docs/runbooks/cdc-dms-stall.md`, `docs/runbooks/pipeline-alarms.md`
- **Evidence:** `docs/incidents/2026-11-10-cdc-dms-stall-drill.md`

## Context
Work package E (Week 8) needs monitoring that detects a controlled CDC failure, then recovery and
an incident report. Before this, only failures inside Step Functions sent an alert (an SNS step).

The owner decided on 2026-11-10:
- the drill is a **silent DMS stall**;
- scope is monitoring plus the drill: alarms, a dashboard, the drill, the report and runbooks;
- the alarms are built first, then one CDC session.

A stopped DMS task is the failure the pipeline cannot report: every scheduled run succeeds with
nothing to apply.

## Decision
1. **CDC metrics from the apply job.** Namespace `ProcureLake/CDC`, dimensions `Environment` and
   `Table`, one set per applied batch:
   - `SourceToSilverSeconds`: from the oldest CDC event's source commit to the Silver commit.
     It is the first leg of the freshness SLI. It is computed as epoch microseconds in Spark,
     because collected timestamps take the driver's local time zone.
   - `AppliedEvents`, `RejectedEvents`, `HeldKeys` and `BlockedBatches`.

   Tables with no new files publish nothing.
2. **Alarms in `dev/observability`.**
   - Failed, timed-out or aborted executions on each of the four state machines; missing data is
     not an alarm.
   - CDC freshness over 900 s, and CDC rejected, held or blocked over 0. These use Metrics
     Insights queries across all tables.
   - Every alarm notifies the alerts topic on ALARM and on OK, and names its runbook.
3. **DMS alarms in `dev/cdc`**, created and destroyed with the task.
   - `CDCLatencySource` over 300 s for 5 of 5 one-minute periods, with **missing data
     breaching**, because a stopped task publishes nothing.
   - An EventBridge rule for `DMS Replication Task State Change` on this task, with an input
     transformer naming the runbook.
   - The metric dimensions are `ReplicationInstanceIdentifier` (the instance id) and
     `ReplicationTaskIdentifier` (the task's resource id, the last part of its ARN). This was
     verified with `list-metrics`.
4. **Delivery to the KMS-encrypted topic.**
   - The data key's policy keeps AWS's default root statement unchanged, and allows
     `cloudwatch.amazonaws.com` and `events.amazonaws.com` `kms:GenerateDataKey*` and
     `kms:Decrypt` only via SNS (`kms:ViaService`).
   - The topic policy keeps the default owner statement, and allows those two services from this
     account for alarms and rules named `procurelake-dev-*`.
   - The publishing roles of the export and access check state machines also need the key, which
     an earlier gap had missed.
5. **One operations dashboard**, `procurelake-dev-operations`. It shows alarm status, state
   machine outcomes, CDC latency and event counts, DMS latency, and Athena bytes scanned per
   workgroup for cost.

## Consequences
- **The drill met the gate.**
  - EventBridge reported the stop within the minute, and the latency alarm 11.8 minutes after
    it. Both scheduled runs during the stall succeeded.
  - Recovery needed one command (`resume-processing`), and all 25 stalled changes reached Gold
    5.8 minutes later, with no loss and no duplicates.
  - The freshness alarm flagged the backlog batch: 921.7 s against 900 s.
- **Three tuning findings stay open** (open question 39):
  - the latency alarm was slower than its configuration suggests;
  - it fires once when the stack is created;
  - the freshness alarm cleared by itself 15 minutes after the late batch, with no newer
    batch, so its OK means only that time passed.
- **Not built (owner scope):** published dbt docs and lineage, Glue table owners and
  classifications, Iceberg `OPTIMIZE`/`VACUUM` (since built: ADR 0033), and a cost report. Lineage today is the dbt
  manifest uploaded per run (`dbt/runs/<run id>/manifest.json`), and cost is the budget alerts
  plus the dashboard's Athena widget.
- **Cost:** 8 standard alarms and 2 more while CDC is up, one dashboard, about 30 custom metric
  series, and per-batch `PutMetricData` calls. All small; not measured separately.
