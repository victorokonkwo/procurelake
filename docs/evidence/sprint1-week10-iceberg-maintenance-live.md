# Evidence: Week 10, Iceberg maintenance (live, dev)

**Scope:** work package G, the Iceberg maintenance item, on 2026-11-11 (ADR 0033):
- the daily maintenance Glue job, run by hand once;
- the dbt compaction post-hook on the four tenant-facing marts;
- tenant isolation on the rewritten files.

The tenants are fictional and synthetic, not SOVRA customers.

## 1. Maintenance job, first run

`make iceberg-maintenance-run`:
- run `jr_71592f15…20d9b`, `SUCCEEDED`;
- 207 s, 414 DPU-seconds (about $0.05 at $0.44/DPU-hour, Glue 5.1, ca-central-1).

Report: `s3://procurelake-dev-artifacts-…/iceberg_maintenance/run_date=2026-11-11/jr_71592f15….json`,
with a local copy at `build/evidence/iceberg_maintenance_run1.json` (not committed). `failed: []`,
`skipped: []`.

The 7 CDC Silver tables took and released their DynamoDB leases under the `LeadingKeys`-scoped
policy.

| Table | Snapshots | Data files | Delete files | Records read | Live rows | Bytes |
|---|---|---|---|---|---|---|
| `mart_actual_spend` | 48 → 49 | 47 → 1 | 47 → 0 | 82,054 → 1,745 | 1,745 = 1,745 | 876,640 → 14,530 |
| `mart_supplier_concentration` | 47 → 48 | 47 → 1 | 46 → 0 | 5,781 → 123 | 123 = 123 | 354,176 → 5,946 |
| `mart_procurement_cycle_time` | 48 → 49 | 47 → 1 | 47 → 0 | 14,479 → 308 | 308 = 308 | 290,683 → 4,235 |
| `mart_awarded_value` (compacted earlier the same day) | 5 → 5 | 1 → 1 | 0 → 0 | 594 | 594 = 594 | 7,758 |
| 7 CDC Silver tables + `silver_tenants` | unchanged (5–10) | 1–2 | 0 | unchanged | unchanged | unchanged |
| `silver_contract_intelligence` | 0 (empty table) | 0 | 0 | 0 | 0 | 0 |

**Why VACUUM expired nothing on this run:**
- Gold: the oldest remaining snapshot of `mart_actual_spend` was committed 2026-11-10
  20:21 UTC, under the 1-day retention.
- Silver: no table had more than 10 snapshots, the minimum kept.
- The properties were written. Gold `SHOW TBLPROPERTIES` shows 86400/5. The Silver
  `metadata.json` has `history.expire.max-snapshot-age-ms = 604800000` and
  `history.expire.min-snapshots-to-keep = 10`. Athena's `SHOW TBLPROPERTIES` lists only the
  second.

Expiry itself was shown earlier the same day, as the data engineer, on `mart_awarded_value`
(51 → 5 snapshots with a 2-hour age).

**Expiry by the job role, observed 2026-11-12.** The first scheduled run (EventBridge Scheduler,
03:30 America/Toronto; run `jr_0771a0d0…`, SUCCEEDED in 203 s, 406 DPU-seconds) reported every
table `maintained`:

| Table | Snapshots before → after | Why |
|---|---|---|
| `mart_supplier_concentration` | 50 → 5 | Gold: older than 1 day, keep at least 5 |
| `mart_procurement_cycle_time` | 51 → 5 | same |
| `mart_actual_spend` | 51 → 5 | same |
| `mart_awarded_value` | 7 → 5 | same |
| Silver tables | 5–10 → unchanged | Silver keeps 7 days and at least 10 |

Report: `s3://<artifacts>/iceberg_maintenance/run_date=2026-11-12/jr_0771a0d0….json`.

## 2. dbt post-hook

`make dbt-build DBT_ARGS="--select mart_actual_spend mart_awarded_value mart_supplier_concentration mart_procurement_cycle_time"`:
`PASS=30 ERROR=0`.

The newest two snapshots of each mart afterwards:

| Mart | Merge snapshot | Post-hook snapshot |
|---|---|---|
| `mart_actual_spend` | `overwrite`: +1 data, +1 delete, total 2 data | `replace`: +1, −2 data, −1 delete, total 1 |
| `mart_awarded_value` | `overwrite`: +1 data, +1 delete, total 2 data | `replace`: +1, −2 data, −1 delete, total 1 |

All four marts ended at 1 data file and 0 delete files, with the same row counts (1,745 / 594 /
123 / 308).

## 3. Tenant isolation after the rewrite

- **First `make test-aws`:** 25 passed, 2 failed (`test_exports_hold_only_the_tenants_rows`
  for `mart_awarded_value` and `mart_actual_spend`).
  - Every exported row carried the right tenant. Only the row count differed: 1,173 exported
    against 1,162 now.
  - The latest export was `export_date=2026-11-10/run=manual-20261110T211943Z`, written before
    the CDC stall drill (23:38 UTC) changed the marts. The export was stale; maintenance did not
    change any row count (table above).
- **`make tenant-exports-run`, then `make test-aws`:** **27 passed** (66.5 s). This covers
  tenant row filters, denied paths, exports and the admin product on the compacted marts.
