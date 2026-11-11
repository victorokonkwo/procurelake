# ADR 0034: Benchmarks in an isolated namespace; partition evolution in Spark

- **Status:** Accepted. The partitioning decision itself, open question 13, is still open; this
  ADR records the evidence for it.
- **Date:** 2026-11-11
- **Implementation:**
  - Snapshot and Silver namespaces: `source_system` in
    `src/procurelake/ingestion/connectors/postgres_snapshot.py`, the ingestion CLI, and
    `src/procurelake/lakehouse/silver/{job,pipeline}.py`
  - Silver schema: `CDC_COLUMNS` on every Silver table (`src/procurelake/lakehouse/silver/writer.py`)
  - Partition evolution: `src/procurelake/lakehouse/partitioning{,_job}.py`,
    `jobs/glue/evolve_partitions.py`
  - Query benchmark: `src/procurelake/benchmarks/athena_queries.py`,
    `configs/benchmarks/athena_queries.yaml`, `scripts/aws/bench_queries.py`
  - Terraform:
    - `modules/glue_catalog` (`bench_databases`)
    - `live/dev/foundation`: data-engineer access to the bench databases
    - `live/dev/data_platform/bench.tf`: two on-demand Glue jobs
  - Make: `bench-db`, `bench-snapshot`, `bench-silver-run`, `bench-dbt-build`, `bench-queries`,
    `bench-compare`, `bench-partition-evolve`
  - Tests:
    - unit: `tests/unit/benchmarks/`, `tests/unit/lakehouse/test_partitioning.py`,
      `test_snapshot_batch_selection.py`, `tests/unit/ingestion/test_postgres_snapshot_source.py`
    - Spark (CI): `tests/integration/iceberg/test_partition_evolution.py`, plus two tests in
      `test_silver_pipeline.py`
    - Terraform: `bench_jobs_touch_only_the_bench_namespace`, and the two `glue_catalog` runs
- **Evidence:** `docs/benchmarks/2026-11-11-bench-100k.md`

## Context
Work package G needs:
- a bulk-load test at 100,000–1,000,000 synthetic records;
- an analytical query benchmark (duration and bytes scanned);
- Iceberg partition evolution and query pruning.

Acceptance test 12 asks for actual measurements.

The dev Silver and Gold could not hold that dataset. Seven Silver tables are CDC-owned, and the
Silver job refuses snapshots into them (ADR 0028). The tenant products and tests depend on the
dev data too.

Platform behaviour checked against the documentation on 2026-11-11:
- **Athena has no Iceberg partition evolution.** Its Iceberg pages list no `ADD PARTITION FIELD`
  and ask for feedback if you want it. `OPTIMIZE` says nothing about old partition specs.
- **Iceberg (Spark DDL):** `ADD PARTITION FIELD` is metadata-only; existing data stays in the
  old layout. `rewrite_data_files` writes to the current spec by default (`output-spec-id`), and
  `rewrite-all` forces every file.
- **Pruning:** Athena documents hidden partitioning but does not explicitly promise pruning on a
  `months()` transform from a filter on the source column. It was measured instead.

The owner decided on 2026-11-11:
- start at 100k (scale 18), stepping up to 1M if that run is cheap and clean;
- use a **separate bench space**;
- **measure, then decide** partitioning (open question 13).

## Decision
1. **A benchmark namespace end to end.**
   - **Postgres:** the local database `procurelake_bench`.
   - **Bronze and manifests:** the `postgres_bench` source system, so `bronze/postgres_bench/` and
     `pipeline_manifests/postgres_bench/`. The snapshot connector accepts only `postgres` or
     `postgres_<name>`.
   - **Silver:** `procurelake_dev_bench_silver` at `s3://<lake>/bench/silver/`, applied by
     `procurelake-dev-bench-bronze-to-silver`. It runs the same code and wheel as the dev job,
     with `--source-system postgres_bench`.
   - **Gold:** `procurelake_dev_bench_gold` at `s3://<lake>/bench/gold/`, built by dbt through its
     existing schema variables. The AI models are excluded.
   - **Governance:** the bench locations are outside the Lake Formation-registered `gold/`, and
     new databases default to IAM-only access. No tenant role is granted them.
   - **Least privilege:** each bench job role may touch only bench resources (Terraform test).
     The partition job is denied the raw bucket entirely.
2. **Batch selection is per source system.** Both manifest loading and `select_snapshot_batch`
   take `source_system`, so a bench batch can never become the dev job's "latest" batch.
3. **Every Silver table carries the CDC lineage columns** (`_source_commit_ts`,
   `_source_sequence`, `_cdc_op`, `_is_deleted`), created with the table or added with
   metadata-only `ADD COLUMNS`. Snapshot rows leave them null, and staging reads null
   `_is_deleted` as live. This fixes a gap the benchmark exposed: dbt staging reads these
   columns, so a Silver built only from snapshots failed `dbt build` with `COLUMN_NOT_FOUND`.
4. **Partition evolution runs in Spark on Glue** (`procurelake-dev-partition-evolution`):
   - it adds a `years`/`months`/`days` field idempotently, then optionally rewrites every file;
   - it reports data files and bytes per `spec_id` before and after, from the `files` metadata
     table;
   - in dev it is limited to the bench Silver database.
5. **The query benchmark:**
   - records Athena's own statistics for every repeat;
   - refuses reused results, and refuses answers that change between repeats;
   - `compare` refuses layouts whose answers differ, so a speed-up is never reported for a
     different result.

## Consequences
- **Measured at 100k** (details and costs in the benchmark doc):
  - **Load:** 105,725 rows into Bronze in 12 s, Silver in 511 s, Gold in 38.9 s (75/75 dbt
    nodes). Glue cost about $0.12 for the Silver load.
  - **Pruning works in Athena.** After evolution and a full rewrite, a one-quarter invoice query
    scanned 91% fewer bytes and a one-month award query 97% fewer.
  - **No time saved at this volume.** Engine time stayed at 0.5–0.7 s in every layout.
  - **Evolution without a rewrite** changed nothing for existing data.
  - **Costs of the rewrite:** storage grew 10% (invoices) and 36% (awards), and the full-scan
    control read 5% more bytes.
- **Open question 13: Silver already partitions by `tenant_id`** (the Silver pipeline config).
  The open part is time partitioning and Gold. The 100k evidence argues against monthly Silver
  partitions *at this volume*: they produce 60+ files of 30–120 KB each and save nothing
  billable. The 1M run is the deciding measurement.
- **Silver load time is mostly fixed overhead.** A rerun that skipped every table took 487 s,
  because the job reads and validates Bronze before it checks the applied batch. A cheap fix
  (check first) is recorded, not made.
- **Two defects found and fixed while running:**
  - batch selection hard-coded `postgres`, so the first bench run failed;
  - Spark prints identity-only partitioning under a different heading from transforms, so the
    first report showed the starting spec as `[]`.

  Both now have unit tests. The parser fix is not yet deployed; the deployed job read the
  transform form correctly for the rewrite run.
- **Cost of keeping it:** two empty Glue databases, two idle Glue jobs, and a few megabytes of
  bench Iceberg files. All of it is negligible, and it can be dropped after the benchmarks.
