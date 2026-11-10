# ADR 0023: Consistent PostgreSQL snapshot into Bronze (pre-CDC batch path)

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `src/procurelake/ingestion/connectors/postgres_snapshot.py`, `src/procurelake/contracts/postgres.py`

## Context
Week 3 builds Silver from Bronze, and the Week 4 milestone needs tenant Gold models. Operational data only reaches Bronze through AWS DMS in Week 5. The repository plan already includes a `postgres_snapshot` connector. A full snapshot gives Bronze the operational tables now, and becomes the full-load baseline that CDC continues from.

## Decision
1. **Every contracted table is read in one `REPEATABLE READ, READ ONLY` transaction**, in foreign-key order. Each table's `count(*)` is taken in the same snapshot, so a batch succeeds only if landed rows = count = unique primary keys.
2. **Rows are landed as PostgreSQL's `row_to_json` text in JSON Lines** (`part-NNNNN.jsonl`, 50,000 rows per file), under `bronze/postgres/<table>/ingestion_date=…/batch_id=…/`. Numerics and timestamps keep PostgreSQL's exact rendering.
3. **Contract gate:** each table's live columns are compared with its source contract before it is read. Drift fails that table only, and nothing is landed for it. All 8 operational tables now have contracts.
4. **The hand-off point for CDC is recorded:** `pg_current_snapshot()` and `pg_current_wal_lsn()` go into every table's manifest.
5. **Manifests:** one per table (`pipeline_manifests/postgres/<table>/window_start=snapshot/batch_id=…`). All tables in a run share one batch ID.

## Consequences
- **First live run (2026-11-10).** Batch `20261110T093202Z-983c92e5` landed 5,702 rows across 8 tables from the seed-42 dataset (fingerprint `350d064e…`). Every table reconciled, every file's SHA-256 and KMS key were verified in S3, and the rows split between `tenant_alder` and `tenant_birch` as expected. WAL LSN `0/416930B8`.
- **The snapshot was taken from the local Docker database.** The Week 5 RDS source will be loaded from the same deterministic generator, so a snapshot and a later DMS full load of seed 42 are comparable.
- **Tests:** reconciliation, exact decimals, file splitting, schema drift, the read-only/isolation settings, and consistency under a concurrent write. The consistency test was mutation-checked: under `READ COMMITTED` the concurrent row leaks in and the test fails.
