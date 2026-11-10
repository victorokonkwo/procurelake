# Evidence: Week 5, CDC from RDS PostgreSQL into Bronze (C1, C2)

**Scope:**
- The source runs a realistic workload.
- DMS full load + CDC lands every change in Bronze, with source commit timestamps.

**Not yet covered:** the C5 gate (Silver and Gold within 15 minutes, correct under retries and
deletes). That needs the apply engine (C3) and recovery (C4).

| | |
|---|---|
| Date | 2026-11-10 |
| Source | RDS PostgreSQL 17.11, `wal_level=logical`, TLS enforced (a plaintext connection was refused), seeded with seed 42. The fingerprint `350d064e…a596e3` matches a local generation |
| DMS | engine 3.6.1 on dms.t3.small; task `procurelake-dev-cdc-full-load-and-cdc` (full load + CDC) |
| Bronze prefix | `bronze/postgres_cdc/load_id=20261110T130454Z/procurement/<table>/` |

## Full load

| Table | DMS full-load rows | Source rows |
|---|---|---|
| tenants | 2 | 2 |
| agencies | 13 | 13 |
| suppliers | 105 | 105 |
| solicitations | 650 | 650 |
| awards | 628 | 628 |
| purchase_orders | 1,481 | 1,481 |
| invoices | 2,823 | 2,823 |
| contract_documents | 0 | 0 |

Each table landed as one `LOAD00000001.parquet` with these properties:
- **Encryption and versioning:** SSE-KMS with the platform data key, in the versioned raw bucket.
- **Schema (awards):** `Op` string, `_dms_commit_ts` string, business columns. `award_amount` is
  `decimal128(18, 2)`, and the timestamps are `timestamp[us, tz=UTC]`. Every full-load row has `Op = I`.

## CDC

The workload (`make cdc-workload`, seed 7) ran 40 operations, changing 41 rows:

| Table | Inserts | Updates | Deletes |
|---|---|---|---|
| invoices | 10 | 26 | 1 |
| suppliers | | 2 | |
| awards | | 1 | |
| purchase_orders | | 1 | |

The updates include one `settle_purchase_order` transaction that touched invoices and a purchase
order together.

| Check | Result |
|---|---|
| DMS table statistics (inserts/updates/deletes per table) | equal to the workload |
| Bronze CDC rows vs workload, multiset of (table, Op, primary key) | **exact match, 41 = 41**, including the hard delete `invoices D 689f4835…` |
| `_dms_commit_ts` vs the source transaction clock | within 0.010 s for every row |
| Commit → S3 object (`LastModified`) | 58.3–60.7 s (the 60 s CDC batch interval) |

## Failures on the way

Details are in ADR 0027.
1. `dms.t3.micro` is not orderable in ca-central-1.
2. The heartbeat table creation was denied in `public`.
3. HeadBucket was denied by a prefix-conditioned `s3:ListBucket`, so every upload failed.

Each was fixed and re-applied after owner approval. No file was written before the fixes, and none
was overwritten.
