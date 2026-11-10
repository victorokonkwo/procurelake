# Evidence: Week 5, C5 freshness gate measured live in dev

**Scope:** one live, timed `make cdc-freshness` run on 2026-11-10. Workload seed 12, 40 operations.
The run applied the CDC workload to the RDS source and waited for AWS DMS to report every change.
It then ran the CDC apply Glue job and `dbt build`, and checked the final state of every changed
key in Silver and Gold. The gate is the owner decision in ADR 0027: 95% of source changes visible
in Silver and Gold within 15 minutes.

**Result: the freshness gate is met for this run.** p95 is 151 s to Silver and 204 s to Gold,
against 900 s. There were 0 mismatches.

Raw record: `build/evidence/cdc_freshness_20261110T150700Z.json` (not committed).

## Latency (source commit to layer available)

| Layer | Available when | Changes | min | p50 | p95 | max | Gate (p95) |
|---|---|---|---|---|---|---|---|
| Silver | CDC apply run `CompletedOn` | 47 | 148.8 s | 149.8 s | 150.7 s | 150.8 s | 900 s: met |
| Gold | `dbt build` returned | 47 | 201.8 s | 202.8 s | 203.8 s | 203.9 s | 900 s: met |

Percentiles use the nearest-rank method (`procurelake.cdc.freshness`), so each value is an
observed latency. Commit times come from the RDS transactions. Silver availability is the Glue
run's `CompletedOn`, which is AWS time. Gold availability is the local clock when `dbt build`
returned; in this session it ran about 1.3 s ahead of the RDS commit timestamps, which makes no
difference against the gate.

## The run

| Step | Result |
|---|---|
| Workload | 47 row changes: invoices 37, purchase_orders 7, awards 2, suppliers 1 |
| DMS | Table statistics showed every change at 15:07:23 UTC, 23 s after the run started |
| CDC apply (`procurelake-dev-apply-cdc`) | `jr_e3592d4f…` SUCCEEDED, 120 s, 240 DPU-seconds, with the DMS task ARN (full-load completeness checked) |
| `dbt build` (data-engineer role) | 10 models + 49 tests, PASS=59, 41.5 s |

## Correctness (Athena, as the data-engineer role)

`cdc_freshness.py` checks the latest change per key: deletes must be tombstones in Silver and
absent from Gold, and every other change must show its after-image value in both layers. Result:
**0 mismatches.**

The 47 changes touched 31 distinct keys (some rows changed more than once in the workload). By
last operation in Silver:

| Silver table | I | U | D |
|---|---|---|---|
| silver_invoices | 6 | 15 | 3 |
| silver_purchase_orders | 0 | 4 | 0 |
| silver_awards | 0 | 2 | 0 |
| silver_suppliers | 0 | 1 | 0 |

The 3 deleted invoices (`d1d4d2b3…` tenant_birch, `2b06529a…` and `e4e810cd…` tenant_alder) are
tombstones in `silver_invoices`, still carrying `tenant_id`. Each has 0 rows in `fct_invoice`.

## What this does not show

- **Scheduled latency.** The Glue job and `dbt build` ran as soon as DMS caught up. Once Step
  Functions and EventBridge schedule them (Week 6), the wait for the next scheduled run adds to
  these numbers. Measure the gate again then.
- **Volume.** This was one run of 47 changes. The 1,000–10,000-change incremental CDC test in the
  performance plan has not been run.
- **Retries, live.** Replay after a mid-run failure is tested locally (C4, ADR 0028) but has not
  been run against the deployed job. The second C5 checkbox stays open for that reason.

## Session notes

- An earlier attempt the same morning (seed 11) measured nothing. A manually started
  `apply-cdc` run was still in progress, so its `StartJobRun` call failed with
  `ConcurrentRunsExceededException` (Glue allows one run at a time). Its 48 changes were applied
  by the next run but were not checked.
- Other CDC activity ran on the stack during the session: the DMS task was recreated with a new
  full load at 15:02 UTC, and other workload changes were committed at 15:06:38. No other
  `apply-cdc` or `dbt build` ran between this run's workload and its checks, and the measured
  changes were only this run's (selected by key), so the result above is not affected.
