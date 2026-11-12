# Benchmarks: replay, concurrent tenant access, incremental CDC (2026-11-12)

The remaining rows of the plan's performance table (work package G). Load, query and
partitioning benchmarks are in `2026-11-11-bench-100k-1m.md`. AI extraction is still blocked on
the Bedrock quota (Week 9). All data is synthetic, and both tenants are fictional.

## 1. Reprocessing: replay an already processed batch

**Workload:** the bench namespace at 1M scale: batch `20261111T161553Z-7c9f837f` from
`postgres_bench`, 1,042,769 rows over 7 tables, already applied on 2026-11-11.

**Method:**
1. Record a fingerprint of Silver and Gold with `make bench-queries LABEL=replay_before
   REPEATS=1 QUERIES=configs/benchmarks/replay_checks.yaml`: rows, distinct keys, duplicate rows
   and money totals per table and currency.
2. Re-run the bench Silver job on the same batch (`make bench-silver-run`).
3. Rebuild bench Gold (`make bench-dbt-build`).
4. Record the fingerprint again and run `make bench-compare`, which fails if any answer differs.

**Results:**

| Step | Result |
|---|---|
| Silver replay | Glue run `jr_396838e2…`: SUCCEEDED in 136 s, 273 DPU-seconds. All 7 tables `skipped`: the batch was read and validated again (1,042,769 input rows, 0 rejected), and the write was skipped because each table records the last batch it applied (ADR 0024) |
| Gold rebuild | `dbt build`: PASS=75, WARN=0, ERROR=0, in 59 s |
| Answers before vs after | **Identical**: `bench-compare` passed on all 5 checks |
| Duplicate business keys | **0** in every Silver table and Gold fact, before and after |

**Measured answers** (same before and after):

| Table | Rows | Duplicate rows |
|---|---|---|
| `silver_awards` | 108,357 CAD + 6,646 USD | 0 |
| `silver_invoices` | 487,956 CAD + 27,476 USD | 0 |
| `silver_purchase_orders` / `silver_solicitations` / `silver_suppliers` | 276,419 / 117,000 / 18,900 | 0 |
| `fct_contract_award` / `fct_purchase_order` / `fct_invoice` | 115,003 / 276,419 / 515,432 | 0 |

Money totals were also identical:
- `mart_awarded_value`, award totals: CAD 41,069,026,943.43 and USD 2,483,639,851.39.
- `mart_actual_spend`, paid invoices: CAD 21,657,682,809.25 and USD 1,296,517,593.05.

Both are kept in source currency and never summed across currencies.

**What this does and doesn't show:**
- **The snapshot path:** replaying a batch is a no-op by design. An older batch is refused, which
  was already shown in a test (`StaleBatchError`).
- **The CDC path:** replaying the same DMS files is covered by the CDC apply tests and the
  Week 5 live runs (ADR 0028). Its merge orders by commit timestamp and sequence, so a re-applied
  change is not a new version.

Evidence (local, git-ignored):
- `build/evidence/bench_queries_replay_{before,after}.json`
- `replay_silver_summary.json`
- `replay_dbt_build.log`

## 2. Concurrent access: two tenant query sessions

**Workload:** dev Gold, the four tenant-facing marts, queried through Lake Formation row filters
(ADR 0030). Each tenant assumes its own role and uses its own Athena workgroup.

**Method:** `make bench-concurrent-tenants ROUNDS=3`
(`src/procurelake/benchmarks/concurrent_tenants.py`).
- Each tenant runs `select tenant_id, count(*) … group by 1` on each mart, three rounds.
- **Sequential phase:** Alder's rounds, then Birch's.
- **Concurrent phase:** both tenants at once, one thread each.
- In total, 48 queries. Latency is Athena's own `Statistics`; result reuse is off, and any reuse
  fails the run.
- Every result must contain only the querying tenant's `tenant_id`, and both phases must give the
  same answer per tenant and mart.

**Results (2026-11-12):**

| Phase | Tenant | Queries | Engine ms (median) | Queue ms (median) | Total ms (median / max) |
|---|---|---|---|---|---|
| Sequential | `tenant_alder` | 12 | 829 | 56 | 942 / 2,823 |
| Sequential | `tenant_birch` | 12 | 722 | 53 | 827 / 2,341 |
| Concurrent | `tenant_alder` | 12 | 721 | 56 | 827 / 995 |
| Concurrent | `tenant_birch` | 12 | 747 | 53 | 859 / 1,450 |

- **Isolation held under concurrency:** all 48 results contained only the querying tenant's
  rows, and the answers were identical across phases.
- **Latency:** running both sessions at once did not slow either tenant at this volume. The
  medians are within run-to-run noise (about 100 ms), and queue time stayed around 55 ms. Each
  tenant has its own workgroup, so neither queues behind the other.
- **Not shown:** behaviour at Athena's account-level concurrency quota, which two sessions do not
  approach.

Evidence: `build/evidence/bench_concurrent_tenants.json` (every query id and its statistics).

## 3. Incremental CDC (1,000–10,000 changes)

**Workload:** the dev CDC stack on 2026-11-12:
- RDS PostgreSQL 17.11 `db.t4g.micro` → DMS `dms.t3.small` → Bronze Parquet;
- the `cdc_processing` schedule, every 5 minutes: CDC apply Glue job, then dbt on Fargate.

The generator's mixed operations (inserts, updates, cancellations, payments, deletes) ran with
seeds 102 and 103. An operation can touch zero or several rows, so 1,000 operations gave
599 row changes and 10,000 gave 5,929.

**Method:** `make cdc-freshness-scheduled SEED_WL=… OPS=…`.
- The harness commits the workload, then starts nothing; the schedule runs the pipeline, as in
  production.
- It polls Silver and Gold every 30 s until every changed key shows its final state: the
  after-image value, or a tombstone in Silver and absence from Gold for deletes. The Athena
  lookups are split into 2,000-key chunks under the 262,144-byte query limit.
- Latency per change is commit time → the first poll that shows **every** change, against the
  15-minute gate (95% of changes in Gold, ADR 0027).

**Results:**

| Workload | Row changes | Commit window | Correct in Silver and Gold | Silver p50 / p95 | Gold p50 / p95 / max | Gate (p95 ≤ 900 s) |
|---|---|---|---|---|---|---|
| 1,000 operations | 599 (458 invoices, 55 awards, 54 suppliers, 32 POs) | 40 s | **599 / 599**, 0 mismatches | 636 / 654 s | 718 / 736 / 738 s | **Met** |
| 10,000 operations | 5,929 (4,456 invoices, 610 suppliers, 496 awards, 367 POs) | 6 min 20 s | **5,929 / 5,929**, 0 mismatches | measured at Gold's poll, see below | 1,163 / 1,338 / 1,356 s | **Missed** |

**Pipeline runs that carried them:**

| Workload | Run start | CDC apply (Glue) | Run end (dbt done) |
|---|---|---|---|
| 1,000 | 15:58:40 | 15:58:41–16:01:25, 152 s, 304 DPU-s | 16:06:13 |
| 10,000 (part) | 16:08:40 | 16:08:41–16:10:57, 129 s | 16:15:33 |
| 10,000 (rest) | 16:18:41 | 16:18:41–16:20:52, 124 s | 16:26:15 |

The 15:53, 16:03, 16:13 and 16:23 ticks each ended within 0.2 s: a run was still active, so the
tick did nothing (the overlap guard, ADR 0029).

**What the numbers show:**
- **Correctness held at both volumes:** every insert, update, cancellation and delete reached
  Silver and Gold with the right final state. That includes 5,929 keys across four tables, in
  two partial batches.
- **Volume is not the limit at this scale.** The CDC apply took 124–152 s whether a batch
  carried about 600 or about 6,000 changes. Glue start-up and the 1-minute minimum dominate it
  (cost review §2).
- **The schedule is the limit.**
  - A run takes about 7.5 minutes (apply about 2.5, dbt about 5). That is longer than the
    5-minute rate, so every other tick is skipped, and runs start every 10 minutes.
  - A change committed just after a run reads its input therefore waits up to about 10 minutes
    for the next run, then about 7.5 for it to finish. The worst case is about 17.5 minutes
    whatever the volume.
  - The 1,000-operation workload was committed 7 minutes before a run started and met the
    gate. The 10,000-operation workload's last commits (16:10:33) just missed the 16:08 run:
    15.7 minutes to Gold (16:26:15) for the latest commit.
- **The harness overstates the 10,000-operation figures,** because it records one visibility
  time, the moment the last change is visible, for every change.
  - Changes committed before about 16:08 were most likely in Gold when the 16:08 run ended
    (16:15:33). This is inferred from the run times; it was not measured per change.
  - Silver for the last changes was available when the second apply ended, 16:20:52, about
    10.3 minutes after the last commit. The harness's Silver poll reported 16:26:48, the same
    poll as Gold, so its Silver figure for this run is not a Silver measurement.
- **Not shown:** sustained throughput above about 6,000 changes per batch, or DMS behaviour on
  a larger instance.

**Follow-up (open question 40, not applied):** the 15-minute gate is not guaranteed under the
current schedule at any volume. Options:
- shorten the dbt step, for example by building only the models downstream of the tables that
  changed;
- start each run when the previous one ends, instead of on a fixed rate;
- or relax the gate.

A first attempt at 1,000 operations (seed 101, 747 row changes) committed its changes but
stopped before checking them. The harness's manual mode tried to start the CDC apply while the
scheduled run held it (`ConcurrentRunsExceededException`). That is the single-writer guard
working. Those changes were applied by the scheduled runs, but were not verified one by one.

Evidence (local, git-ignored):
- `build/evidence/cdc_freshness_scheduled_20261112T155053Z.json` (1,000)
- `build/evidence/cdc_freshness_scheduled_20261112T160414Z.json` (10,000)
- `cdc_bench_*.log`
