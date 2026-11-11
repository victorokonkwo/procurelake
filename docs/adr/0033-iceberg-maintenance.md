# ADR 0033: Iceberg maintenance: compaction after each dbt merge, and a daily OPTIMIZE/VACUUM job

- **Status:** Accepted
- **Date:** 2026-11-11
- **Implementation:**
  - Logic: `src/procurelake/lakehouse/maintenance.py`, `src/procurelake/lakehouse/maintenance_job.py`
  - Glue entrypoint: `jobs/glue/iceberg_maintenance.py`
  - dbt post-hook: `dbt/macros/optimize_iceberg.sql`, on the four tenant-facing marts
  - Terraform (`infrastructure/live/dev/data_platform`): the `iceberg_maintenance` Glue job, its
    IAM, the EventBridge Scheduler schedule, and the failure rule to `pipeline_alerts`
  - Tests: `tests/unit/lakehouse/test_iceberg_maintenance.py`,
    `tests/data_platform.tftest.hcl` (two runs)
  - Runbook: `docs/runbooks/iceberg-maintenance.md`
- **Evidence:** `docs/evidence/sprint1-week10-iceberg-maintenance-live.md`

## Context
Work package G (Week 10) includes Iceberg maintenance. ADR 0031 left `OPTIMIZE`/`VACUUM` out of
scope. A probe of dev on 2026-11-11 showed the problem it would solve:

- **Gold marts.** The four tenant-facing marts use dbt's Iceberg `merge` strategy (ADR 0030).
  Athena's MERGE is merge-on-read: every run adds one data file and one position-delete file.
  After 47 runs, `mart_actual_spend` held 47 data files and 47 delete files. A reader read 82,054
  records to return 1,745 rows. The table also had 48 snapshots, all kept.
- **Silver.** The Spark jobs write copy-on-write (2 data files per table, no delete files) and
  stay small. But nothing expired their snapshots or removed unreferenced files.
- **dbt core tables** (`table` materialization) are rebuilt with a single location; dbt-athena
  removes the old one. They need no maintenance.

Behaviour verified against the Athena documentation, and live where the documentation is
unclear:

- `OPTIMIZE t REWRITE DATA USING BIN_PACK` rewrites small files and applies their delete files.
  A `WHERE` clause may use only partition columns.
- `VACUUM t` expires snapshots older than `vacuum_max_snapshot_age_seconds` (default 432000)
  while keeping `vacuum_min_snapshots_to_keep` (default 1), then removes unreferenced files.
  Without `s3:DeleteObject` it reports success and deletes nothing.
- **Lake Formation.** The documentation says DDL is not supported on Lake Formation-registered
  tables. On dev, `mart_awarded_value` is LF-registered (hybrid mode, ADR 0030), and the data
  engineer ran `ALTER TABLE SET TBLPROPERTIES`, `OPTIMIZE` and `VACUUM` on it. The data cells
  filters survived, and the tenant access check passed afterwards.

The owner decided on 2026-11-11:
- **Retention:** Silver 7 days with at least 10 snapshots; Gold marts 1 day with at least 5.
- **Mechanism:** a dbt post-hook plus a daily job.

## Decision
1. **Compact Gold marts in the dbt run that writes them.** A post-hook
   `optimize {{ this }} rewrite data using bin_pack` runs on incremental runs. It runs after
   `delete_vanished_keys`, so those deletes are compacted too. Readers never see more than one
   merge's worth of files.
2. **A daily Glue job for everything else.** The job `procurelake-dev-iceberg-maintenance`
   runs on Glue 5.1 Spark (2 DPU), reusing the `procurelake` wheel. A Glue Python shell would be
   Python 3.9, against the 3.11 fixed in ADR 0015. It issues Athena statements; Spark itself is
   unused. For each table in the Terraform lists (8 Silver, 4 Gold marts) it:
   1. sets the layer's retention with `ALTER TABLE … SET TBLPROPERTIES`, which re-applies it to
      tables dbt or Glue recreated;
   2. runs `OPTIMIZE … REWRITE DATA USING BIN_PACK`;
   3. runs `VACUUM`.
3. **Never race a writer.**
   - The 7 CDC Silver tables are maintained only while holding the table's DynamoDB lease, the
     one the CDC apply job takes (ADR 0028). If the lease is held, the table is skipped this run,
     and that is not a failure.
   - `ICEBERG_COMMIT_ERROR` is retried up to 3 attempts.
   - The live row count (records − position deletes − equality deletes, from the snapshot
     summary) must be equal before and after; otherwise the table fails.
4. **Report every run.** Health before and after each table, from the current snapshot summary
   and the `$snapshots` count, goes to
   `s3://<artifacts>/iceberg_maintenance/run_date=<date>/<run id>.json`. Athena's `$files` lists
   data files only, so delete files are read from the summary. Every table is attempted; the job
   fails if any table failed.
5. **Schedule and alerting.** EventBridge Scheduler runs it at 03:30 America/Toronto. A
   `Glue Job State Change` rule (`FAILED`, `TIMEOUT`, `ERROR`) publishes to the pipeline alerts
   topic, with the runbook in the message.
6. **Least privilege.** The job role has:
   - Athena on the engineering workgroup only;
   - Glue `GetTable`/`UpdateTable` on Silver and Gold;
   - S3 read, write and delete on `silver/*` and `gold/*` of the lake bucket, and an explicit
     deny on the raw bucket. Bronze stays immutable, and a Terraform test asserts the deny;
   - DynamoDB access only to lease items of the leased tables (`dynamodb:LeadingKeys`).

## Consequences
- **Measured, first run (2026-11-11, 207 s, 414 DPU-seconds, about $0.05 at $0.44/DPU-hour):**
  - The three bloated marts went from 47 data files and 46–47 delete files to 1 data file and 0
    delete files, with identical live rows:
    - `mart_actual_spend`: 876,640 → 14,530 bytes; 82,054 → 1,745 records read;
    - `mart_supplier_concentration`: 5,781 → 123;
    - `mart_procurement_cycle_time`: 14,479 → 308.
  - Silver and the already-compact `mart_awarded_value` were unchanged: no new snapshot.
- **The post-hook works.** The next `dbt build` of the marts (30/30 passed) committed each merge
  (1 data + 1 delete file), then a `replace` back to 1 data file and 0 delete files.
- **Tenant isolation held** on the rewritten files: `make test-aws` passed 27/27 after the
  exports were refreshed. The first attempt failed 2 count checks because the latest export
  predated the CDC stall drill, not because of maintenance.
- **Snapshot expiry under the job role is not yet observed.** On the first run, every Gold
  snapshot was under a day old and every Silver table had ≤ 10 snapshots, so `VACUUM` had
  nothing to expire. Expiry was shown earlier the same day as the data engineer (51 → 5
  snapshots on `mart_awarded_value`). The first scheduled run is 2026-11-12 03:30 Toronto time;
  its report will show it.
- **Kept files.** Files referenced by kept snapshots stay in S3 until those snapshots expire.
  The lake bucket is versioned, so files `VACUUM` deletes remain as noncurrent versions for 30
  days.
- **Snapshot growth on Gold.** Each dbt run adds 2 snapshots per mart. With `cdc_processing`
  every 5 minutes that is up to 576 a day per mart while CDC is up, all expired after a day.
- **Gold is not leased.** A dbt run during maintenance can cause a commit retry, or fail the
  row-count check. Either fails loudly and a rerun fixes it. The 03:30 schedule is outside CDC
  sessions.
- **Athena quirk.** `SHOW TBLPROPERTIES` did not list `vacuum_max_snapshot_age_seconds` on the
  Spark-created Silver tables, but the metadata has `history.expire.max-snapshot-age-ms`.
- **Not done in this ADR** (rest of the work package G maintenance item):
  - partition evolution;
  - query-pruning measurements;
  - target data-file sizes at larger volumes. At dev volume every table compacts to one file.
