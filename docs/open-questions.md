# Open questions

These are decisions the planning documents **do not make**. Agents and contributors must not pick an answer silently. Resolve each one with the project owner, record the decision (as an ADR where appropriate), and then remove it from this list.

## Tooling and versions
1. ~~Python version and Glue runtime~~ **Resolved: ADR 0015** (Glue 5.1, Python 3.11, PostgreSQL 17).
2. **Pinned versions** for dbt-core, dbt-athena, and dbt packages (`packages.yml`). (Python dependencies are pinned in `uv.lock`. Terraform and the AWS provider are pinned in ADR 0019.)
3. **Iceberg integration in Glue:** which Glue job configuration is used to read and write Iceberg tables (check the current Glue docs for the chosen Glue version).
4. ~~Local Glue testing approach~~ **Resolved: ADR 0024** (local PySpark 3.5.6 + Iceberg 1.10.0 + Java 17, matching Glue 5.1; a live Glue run verifies the AWS side).
5. **Security scanning tools** for `ci-security.yml` (Python dependency, Terraform/IaC, and full-history secrets scanning). The pre-commit hooks cover only private keys and AWS credentials in staged files.
6. ~~Terraform remote state backend~~ **Resolved: ADR 0019** (S3 bucket, native `use_lockfile` locking).

## Data model
7. ~~Full OLTP column lists~~ **Resolved: ADR 0016.**
8. **Late-arrival policy:** the lookback window for incremental dbt models and how late or out-of-order CDC events are resolved (for example, ordering by source commit timestamp plus a tiebreaker).
9. **Delete semantics in Gold:** hard removal vs. a soft-delete flag in current-state models, and how tombstones propagate from Silver.
10. **Exchange-rate source and conversion policy** for any cross-currency reporting.
11. **Supplier canonicalization rules** for `dim_supplier` (one canonical supplier), especially across tenants and public reference data.
12. **How public reference data relates to tenant data.** Is it joined into tenant products, kept separate, or exposed only to the admin product?
13. **Partitioning strategy** for each Silver and Gold Iceberg table.

## Platform
14. **Freshness objective for the C5 gate:** the plan's initial SLI target is 15 minutes for 95% of changes. Confirm it is the gate threshold.
15. **Locking mechanism** to prevent overlapping writers per target table (for example, a DynamoDB conditional write). Not yet specified.
16. **Where dbt runs:** a container (`jobs/containers/dbt_runner/`) on which compute service, triggered by Step Functions.
17. **Networking:** VPC layout for RDS and DMS that avoids an unnecessary NAT gateway, and how Glue reaches the sources.
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

## Naming inconsistencies between the source documents
23. ~~Cycle-time mart name~~ **Resolved: ADR 0017** (`mart_procurement_cycle_time`).
24. ~~Bronze Postgres prefix~~ **Resolved: ADR 0017** (`bronze/postgres/`). How full-load and CDC output are laid out is deferred to ADR 0014 (Week 5).
25. ~~Terraform layout~~ **Resolved: ADR 0017** (`infrastructure/live/`).
26. ~~Package root~~ **Resolved: ADR 0017** (`src/procurelake/`).
