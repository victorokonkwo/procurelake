# ADR 0027: CDC source (RDS PostgreSQL) and AWS DMS into Bronze

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:**
  - Terraform: `infrastructure/modules/{cdc_network,rds_postgresql,dms_s3_cdc,dms_account_roles}` and `infrastructure/live/dev/cdc/`
  - Python: `src/procurelake/cdc/source_setup.py` and `src/procurelake/data_generator/workload.py`
  - Scripts: `scripts/aws/cdc_*.py`

## Context
Week 5 (C1, C2) replicates the operational database into Bronze with AWS DMS (full load + CDC).
On 2026-11-10 the project owner decided:
- networking (open question 17): public RDS limited to the operator's IP
- the DMS size: smallest instance
- the lifecycle: a separate stack destroyed after each session
- the C5 gate and the writer lock (open questions 14 and 15): 15 minutes for 95% of changes, and a DynamoDB lease
- where CDC lands: Bronze, with deletes denied

## Decision
1. **Disposable stack.** `live/dev/cdc` holds the VPC, RDS, secrets and DMS. It costs about $0.08/hour while
   it exists (Pricing API: RDS db.t4g.micro $0.018, DMS dms.t3.small $0.04, interface endpoint $0.011,
   plus storage, public IPv4 and secrets). Destroy it after every session with `make cdc-down`; Bronze
   and Terraform state are kept. The roles DMS looks up by name (`dms-vpc-role`,
   `dms-cloudwatch-logs-role`) are in foundation.
2. **Network, with no NAT:**
   - RDS is in public subnets, and its security group admits only the operator CIDR (validated to
     /24 or narrower) and the DMS security group. Plaintext connections are refused.
   - DMS is in private subnets with no default route. It reaches S3 through a gateway endpoint and
     Secrets Manager through one interface endpoint.
3. **Source:**
   - PostgreSQL 17.11 on `db.t4g.micro`, encrypted with the data key, with `rds.logical_replication=1`
     and `rds.force_ssl=1`.
   - The master password is RDS-managed.
   - DMS cannot use RDS-managed secrets (AWS DMS User Guide), so it connects as
     `procurelake_dms`. That user's password comes from an ephemeral Terraform value written with
     `secret_string_wo`, so it is in neither the plan nor the state.
   - `make cdc-source-setup` checks `wal_level=logical`, applies the migrations, loads the seeded
     dataset (the same fingerprint as local), and grants the DMS user only `rds_replication`, SELECT on
     `procurement`, and CREATE on a dedicated `awsdms` schema.
4. **DMS:**
   - Engine 3.6.1 (PostgreSQL 17 needs 3.5.4 or later) on `dms.t3.small`. `dms.t3.micro` is priced but not
     orderable in ca-central-1 for 3.5.4 or 3.6.1.
   - Plugin `test-decoding`, `CaptureDdls=false` (no event triggers or DDL tables on the source), and
     a heartbeat in the `awsdms` schema.
   - Full load + CDC to S3 as Parquet 2.0. Each row has `Op` (I/U/D), and
     `_dms_commit_ts` (`TimestampColumnName`) holds the source commit time for CDC rows and the task
     start time for full-load rows (`UseTaskStartTimeForFullLoadTimestamp`).
   - SSE-KMS with the data key; `TargetTablePrepMode=DO_NOTHING`; a CDC batch interval of 60 s.
5. **Bronze immutability over the DMS guidance:**
   - The DMS docs say "Don't enable versioning for S3" and list `s3:DeleteObject` for the role. Our
     Bronze bucket stays versioned, and the DMS role is **denied** deletes under `bronze/` (owner decision).
   - The full load and CDC both worked with this denial.
   - Each stack instance writes under `bronze/postgres_cdc/load_id=<stack creation time>/`, so a later
     session's full load (`LOAD00000001.parquet`) can never overwrite an earlier one. A
     DMS-side delete (for example after a source TRUNCATE) would fail the task instead of erasing Bronze.
6. **Ordering:** Parquet output does not preserve transaction order across tables (DMS docs). The
   apply engine orders changes per table by `_dms_commit_ts` and must not assume cross-table order.
   The workload's `settle_purchase_order` changes two tables in one transaction, so this is exercised.
   Transaction-ordered CSV output (`PreserveTransactions`) is the documented alternative if cross-table
   order is ever required (ADR 0014, to be written).

## Consequences
- **Evidence:** `docs/evidence/sprint1-week5-cdc-bronze-live.md`.
- **First-run failures** (each fixed and covered by a test):
  - `dms.t3.micro` was not orderable.
  - The heartbeat could not create its table in `public` (PostgreSQL 15+ default).
  - HeadBucket is authorised as `s3:ListBucket` with no `s3:prefix`, so a prefix-conditioned
    ListBucket blocked every upload. The role now has unconditioned ListBucket on the raw bucket, as
    the DMS docs list; it still cannot read or delete objects.
- **Known gaps:**
  - The operator scripts use `sslmode=require` and do not verify the RDS certificate against the
    RDS CA bundle.
  - DMS logs go to `dms-tasks-<instance>` with default retention.
- **Next (C3–C5):** the CDC apply engine into Silver, with a DynamoDB checkpoint and lease, replay
  and recovery tests, and the freshness gate.
