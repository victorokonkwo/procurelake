# Open questions

These are decisions the planning documents **do not make**. Agents and contributors must not pick an answer silently. Resolve each one with the project owner, record the decision (as an ADR where appropriate), and then remove it from this list.

## Tooling and versions
1. ~~Python version and Glue runtime~~ **Resolved: ADR 0015** (Glue 5.1, Python 3.11, PostgreSQL 17).
2. ~~Pinned versions for dbt~~ **Resolved: ADR 0026** (dbt-core 1.12.6, dbt-athena 1.11.1; no dbt packages yet: pin any added package in `packages.yml` with an ADR).
3. **Iceberg integration in Glue:** which Glue job configuration is used to read and write Iceberg tables (check the current Glue docs for the chosen Glue version).
4. ~~Local Glue testing approach~~ **Resolved: ADR 0024** (local PySpark 3.5.6 + Iceberg 1.10.0 + Java 17, matching Glue 5.1; a live Glue run verifies the AWS side).
5. **Security scanning tools** for `ci-security.yml` (Python dependency, Terraform/IaC, and full-history secrets scanning). The pre-commit hooks cover only private keys and AWS credentials in staged files.
6. ~~Terraform remote state backend~~ **Resolved: ADR 0019** (S3 bucket, native `use_lockfile` locking).

## Data model
7. ~~Full OLTP column lists~~ **Resolved: ADR 0016.**
8. **Late-arrival policy:** the lookback window for incremental dbt models and how late or out-of-order CDC events are resolved (for example, ordering by source commit timestamp plus a tiebreaker). *Week 4 uses full Iceberg rebuilds (ADR 0026), so this is needed when incremental merge arrives with CDC in Week 6.*
9. ~~Delete semantics in Gold~~ **Resolved: ADR 0026** (hard removal; history via Iceberg snapshots).
10. **Exchange-rate source and conversion policy** for any cross-currency reporting.
11. **Supplier canonicalization rules** for `dim_supplier` (one canonical supplier), especially across tenants and public reference data.
12. **How public reference data relates to tenant data.** Is it joined into tenant products, kept separate, or exposed only to the admin product?
13. **Partitioning strategy** for each Silver and Gold Iceberg table.

## Platform
14. ~~Freshness objective for the C5 gate~~ **Resolved (owner, 2026-11-10): 95% of source changes visible in Silver and Gold within 15 minutes** (ADR 0027).
15. ~~Locking mechanism~~ **Resolved (owner, 2026-11-10): a per-table lease via a DynamoDB conditional write**, in the same table as the checkpoints. To be implemented with the apply engine (C3).
16. ~~Where dbt runs~~ **Resolved: ADR 0029** (ECS Fargate task from `jobs/containers/dbt_runner`, started by Step Functions; runs as the data-engineer role).
17. ~~Networking~~ **Resolved: ADR 0027** (public RDS limited to the operator IP; DMS in private subnets with S3 gateway + Secrets Manager interface endpoints; no NAT).
18. **Bedrock model choice** and Region availability in `ca-central-1` for that model. Also whether Textract features needed are available in `ca-central-1`.
19. **Human review queue:** where it lives and how approvals are recorded.
20. **BI / dashboard tool** for the tenant and admin dashboards.
21. **Snapshot retention period** for Iceberg `VACUUM`.
22. ~~Actual S3 bucket names~~ **Resolved: ADR 0019** (`procurelake-<env>-<role>-<account>-<region>`).
27. **Least-privilege deploy role.** `procurelake-github-deploy-dev` uses `AdministratorAccess` (scoped by trust to the GitHub `dev` environment only). Replace it with a scoped policy in the Week 10 security review.
28. **S3 Object Lock for Bronze.** Today immutability relies on versioning plus an IAM delete denial. Decide whether to adopt Object Lock, which must be enabled when a bucket is created, before any production use.
29. **Human AWS access.** Dev was deployed with IAM user `victoroko` (a long-lived access key plus temporary `AdministratorAccess`). Move to IAM Identity Center (SSO), remove the user's `AdministratorAccess`, and rotate or delete the access key.
30. **Re-collection policy for settled windows.** USAspending records can change after the 10-day settling period. Decide on a periodic re-sweep (for example, the trailing 30 days weekly) and how Silver picks among several batches for the same window (latest succeeded batch, or dedup by `generated_internal_id` plus `Last Modified Date`).

31. **Glue 6.0.** The AWS Pricing API lists Glue 6.0+ ETL at $0.308/DPU-hour in ca-central-1 against $0.44 for Glue 5.1 (checked 2026-11-10). Moving changes the Spark/Python/Iceberg versions fixed in ADR 0015, so the local test stack would change too. Decide before scaling the Silver jobs.
32. **Glue dependency source.** The Silver job installs exact pins from PyPI at every run start (ADR 0025). Decide whether to host the dependency wheels in the artifacts bucket (`.gluewheels.zip` with `--no-index`) to remove the runtime dependency on PyPI.
33. **Glue log encryption.** `/aws-glue/jobs/{error,output}` use CloudWatch default encryption. Decide whether to use a KMS key (needs a Glue security configuration and `logs:AssociateKmsKey`).
34. **USAspending in Silver.** Only the operational tables are in Silver. Decide the Silver grain and dedup for USAspending batches (ties to item 30).

35. **Canonical supplier.** `dim_supplier` is one row per supplier registration within a tenant. Decide whether, and how, suppliers are matched across tenants or names (an entity-resolution policy) before any cross-tenant supplier reporting.
36. **`gold_staging` database.** Staging is ephemeral (ADR 0026), so `procurelake_dev_gold_staging` is empty. Keep it for materialized intermediate models, or remove it (and its IAM grants) in the next foundation change.

37. **RDS certificate verification.** The operator scripts connect with `sslmode=require` and do not verify the server certificate against the RDS CA bundle (ADR 0027). Decide whether to pin the bundle (`verify-full`).
38. **Tenant catalog visibility.** Tenant roles can read Glue metadata (table names and schemas, not rows) for every Gold table (ADR 0030). Decide whether to limit `glue:GetTable(s)` to the tenant-facing tables, or accept schema visibility.
39. **DMS and freshness alarm tuning (Week 8 drill).** Three findings, measured but not explained. (a) The DMS latency alarm (5 of 5 one-minute periods, missing data breaching) went to ALARM 11.8 min after the task stopped, where about 5 min was expected; the EventBridge "task stopped" event arrived within a minute. (b) The same alarm fires once when the stack is created, before the task publishes metrics. (c) The freshness alarm went to ALARM at 23:55:17Z and cleared by itself at 00:10:17Z with no newer batch (last datapoint 23:54Z), so it stays in ALARM about 15 minutes after a late batch whether or not anything changes; an OK email therefore does not mean a later batch was on time. Decide whether to alarm on the stopped event, suppress the startup alarm, and whether the freshness alarm's clear needs to mean more than "time passed".

## Naming inconsistencies between the source documents
23. ~~Cycle-time mart name~~ **Resolved: ADR 0017** (`mart_procurement_cycle_time`).
24. ~~Bronze Postgres prefix~~ **Resolved: ADR 0017** (`bronze/postgres/`). How full-load and CDC output are laid out is deferred to ADR 0014 (Week 5).
25. ~~Terraform layout~~ **Resolved: ADR 0017** (`infrastructure/live/`).
26. ~~Package root~~ **Resolved: ADR 0017** (`src/procurelake/`).
