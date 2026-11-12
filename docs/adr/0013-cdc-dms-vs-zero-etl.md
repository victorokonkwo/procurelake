# ADR 0013: CDC through DMS → S3 → Iceberg, not AWS Zero-ETL (evaluation)

- **Status:** Accepted
- **Date:** 2026-11-12
- **Evaluates:** the plan's Week 10 item "Zero-ETL comparison"; builds on ADR 0027 (DMS into
  Bronze) and ADR 0028 (CDC apply engine).
- **Method:** a documentation review on 2026-11-12, AWS pages linked below. Nothing was
  deployed, so **no Zero-ETL latency or cost here is measured**. The only measured figures are
  this project's own DMS path (ADR 0031, `docs/benchmarks/2026-11-12-replay-concurrency-cdc.md`).

## Context
The job posting names AWS Zero-ETL. ProcureLake replicates the RDS for PostgreSQL source with
DMS (full load + CDC) into immutable Bronze Parquet, and a Glue job merges the changes into
Silver Iceberg tables under a DynamoDB lease and checkpoint. The question is whether a managed
Zero-ETL integration could replace that path, and where it would fit.

## What AWS documents (2026-11-12)

**Targets from RDS for PostgreSQL.**
- RDS zero-ETL integrations replicate into **Amazon Redshift**. The RDS for PostgreSQL to
  Redshift integration is generally available
  ([what's new, 2025-07](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-rds-zero-etl-redshift-generally-available/);
  [supported engines and Regions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.RDS_Fea_Regions_DB-eng.Feature.ZeroETL.html)).
- The RDS "SageMaker lakehouse" target
  ([creating](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.creating-smlh.html))
  writes into a **Redshift managed catalog**, not into Iceberg tables in our own S3 buckets.
  The [RDS features page](https://aws.amazon.com/rds/features/) does not list SageMaker as a
  target for RDS for PostgreSQL.
- **Aurora** PostgreSQL to SageMaker lakehouse does exist
  ([what's new, 2025-10](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-aurora-postgresql-zero-etl-integration-sagemaker/)),
  but needs an Aurora source.
- The DMS-managed zero-ETL also targets **Redshift**
  ([DMS zero-ETL](https://docs.aws.amazon.com/dms/latest/userguide/zero-etl.html)).
- **Glue zero-ETL** can write Iceberg into S3 or S3 Tables, but its sources are DynamoDB,
  Oracle Database@AWS and SaaS applications, not RDS for PostgreSQL
  ([Glue zero-ETL targets](https://docs.aws.amazon.com/glue/latest/dg/zero-etl-target.html),
  [using it](https://docs.aws.amazon.com/glue/latest/dg/zero-etl-using.html)). DynamoDB as a
  source needs point-in-time recovery
  ([DynamoDB to lakehouse](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/amazon-sagemaker-lakehouse-for-DynamoDB-zero-etl.html)).

**Source requirements**
([setting up](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.setting-up.html),
[Redshift requirements and limits](https://docs.aws.amazon.com/redshift/latest/mgmt/zero-etl.reqs-lims.html)):
- a supported minor version (for PostgreSQL 17, 17.3 or later);
- a custom parameter group with `rds.logical_replication=1`, `rds.replica_identity_full=1`,
  `session_replication_role=origin`, `wal_sender_timeout` of 0 or at least 20000,
  `max_wal_senders` and `max_replication_slots` of at least 20, and
  `max_slot_wal_keep_size=-1`;
- automated backups on; same Region as the target; no Multi-AZ DB cluster or read replica as
  the source; no major version upgrade while the integration exists.

**Target requirements:** Redshift Serverless, or provisioned RA3 with at least two nodes;
encrypted; case-sensitive identifiers enabled. The target database is read-only.

**Data limits:**
- every replicated table needs a primary key;
- custom, extension, ENUM and geometric types are not supported, nor are values over 64 KB;
- `numeric` needs an explicit precision, and NaN and ±Infinity are rejected;
- `uuid` arrives as `VARCHAR(36)`, `json` as `SUPER`;
- a table with declarative partitioning or a cascading foreign key goes into a failed state;
- some DDL forces a resync of the table;
- filtering is by database and table name only, with at most 99 patterns and no transforms
  ([filtering](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/zero-etl.filtering.html));
- [history mode](https://docs.aws.amazon.com/redshift/latest/mgmt/zero-etl-history-mode.html),
  which keeps deleted and earlier row versions, is not listed for RDS for PostgreSQL sources.

**Latency:** AWS describes replication "within seconds". This is a description, not an SLA or
a measurement.

**Pricing:** there is no charge for the integration itself. The cost is the Redshift target.
Serverless bills per RPU-hour ([Redshift pricing](https://aws.amazon.com/redshift/pricing/),
[capacity](https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-capacity.html)), with a
default base capacity of 128 RPU. The ca-central-1 RPU price and the smallest base capacity
offered there were not confirmed.

**Not confirmed from the docs:** whether a publicly accessible source instance is allowed; any
instance-class limits; per-GB CDC charges.

## Decision
1. **DMS → Bronze → Glue apply into Iceberg stays the CDC path.** No Zero-ETL integration
   writes RDS for PostgreSQL changes into Iceberg tables in our own S3 lake. The documented
   targets are Redshift or a Redshift managed catalog. Moving to one would mean losing four
   things:
   - **immutable Bronze CDC events** (ADR 0002): the raw change history for replay and audit;
   - **explicit apply semantics** (ADR 0028): commit-order merge, tombstones for hard deletes,
     and replay that adds no duplicate keys;
   - **checkpoint and lease control** (ADR 0007): no overlapping writers, and a checkpoint only
     after a validated write;
   - **Lake Formation on Glue Catalog Iceberg tables** (ADR 0030) as the single
     tenant-isolation mechanism for Gold.
2. **Where Zero-ETL would fit**, recorded for the evolution path, not built:
   - a Redshift serving layer for low-latency operational reporting, fed in parallel to the
     lake, with the lake staying the system of record;
   - an Aurora PostgreSQL source, if the OLTP moves to Aurora and SageMaker lakehouse becomes
     the analytics platform;
   - DynamoDB operational tables into Iceberg through Glue zero-ETL (PITR is already on).
3. **No proof of concept is deployed.** If one is built later, it must be measured on its own
   and not presented as equivalent to the lake path. It would need a Redshift Serverless
   workgroup billed per RPU-hour, a second parameter group on the source, and replica
   identity FULL on every table.

## Consequences
- The CDC freshness numbers in this repo are DMS numbers. Any comparison with Zero-ETL latency
  is open until a proof of concept measures it.
- The source keeps the settings DMS needs (ADR 0027). Zero-ETL's stricter settings
  (`replica_identity_full`, `max_slot_wal_keep_size=-1`, at least 20 replication slots) are
  not applied. Unlimited WAL retention on a slot is a disk risk on a `db.t4g.micro`.
- `V0001__operational_schema.sql` already meets several Zero-ETL constraints:
  - a primary key on all 8 tables;
  - `numeric(18,2)` with explicit precision;
  - no ENUM, geometric or extension types;
  - no declarative partitioning and no cascading foreign keys.

  A later proof of concept would not need schema changes for those. The `uuid` keys would
  arrive as `VARCHAR(36)`.

## Alternatives considered
- **RDS for PostgreSQL → Redshift zero-ETL as the CDC path:** rejected. It moves the system of
  record into Redshift, gives no Bronze event history, and leaves no apply step in our control.
- **Migrate the source to Aurora for SageMaker lakehouse zero-ETL:** rejected for now. It means
  a different, costlier source engine, a managed catalog outside our Iceberg layout, and a
  second isolation model next to Lake Formation on Glue tables.
- **DMS Serverless:** not evaluated here. It changes how DMS is billed and sized, not the
  architecture.
