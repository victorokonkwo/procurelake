# Open questions

These are decisions the planning documents **do not make**. Agents and contributors must not pick an answer silently. Resolve each one with the project owner, record the decision (as an ADR where appropriate), and then remove it from this list.

## Tooling and versions
1. ~~Python version and Glue runtime~~ **Resolved: ADR 0015** (Glue 5.1, Python 3.11, PostgreSQL 17).
2. ~~Pinned versions for dbt~~ **Resolved: ADR 0026** (dbt-core 1.12.6, dbt-athena 1.11.1; no dbt packages yet: pin any added package in `packages.yml` with an ADR).
3. **Iceberg integration in Glue:** which Glue job configuration is used to read and write Iceberg tables (check the current Glue docs for the chosen Glue version).
4. ~~Local Glue testing approach~~ **Resolved: ADR 0024** (local PySpark 3.5.8 (Glue 5.1 runs 3.5.6, same 3.5 line) + Iceberg 1.10.0 + Java 17; a live Glue run verifies the AWS side).
5. ~~Security scanning tools~~ **Resolved: ADR 0037** (owner, 2026-11-11): `ci-security.yml` runs Checkov 3.3.30 (Terraform), pip-audit 2.10.1 (`uv.lock`, hash-checked) and gitleaks v8.30.1 (full history, checksum-verified binary) on PRs, pushes and weekly. Waivers carry reasons: inline per resource, repository-wide in `.checkov.yaml`, by fingerprint in `.gitleaksignore`.
6. ~~Terraform remote state backend~~ **Resolved: ADR 0019** (S3 bucket, native `use_lockfile` locking).

## Data model
7. ~~Full OLTP column lists~~ **Resolved: ADR 0016.**
8. **Late-arrival policy:** the lookback window for incremental dbt models and how late or out-of-order CDC events are resolved (for example, ordering by source commit timestamp plus a tiebreaker). *Week 4 uses full Iceberg rebuilds (ADR 0026), so this is needed when incremental merge arrives with CDC in Week 6.*
9. ~~Delete semantics in Gold~~ **Resolved: ADR 0026** (hard removal; history via Iceberg snapshots).
10. **Exchange-rate source and conversion policy** for any cross-currency reporting.
11. **Supplier canonicalization rules** for `dim_supplier` (one canonical supplier), especially across tenants and public reference data.
12. **How public reference data relates to tenant data.** Is it joined into tenant products, kept separate, or exposed only to the admin product?
13. ~~Partitioning strategy for each Silver and Gold Iceberg table~~ **Resolved: ADR 0034** (owner, 2026-11-11, on the 100k and 1M measurements): tenant-owned Silver tables stay partitioned by `tenant_id` only; Gold stays unpartitioned, as dbt builds it now. Add `months()` on `silver_invoices.invoice_date` and `silver_awards.awarded_at` (metadata change plus one rewrite, `make bench-partition-evolve` shows the mechanics) once a date-filtered Silver scan exceeds Athena's 10 MB per-query minimum. Measured: −91%/−97% bytes but no time or billable saving up to 1M rows; the largest Silver scan was 2.6 MB. Gold was measured at the same sizes, not tested with partitions.

## Platform
14. ~~Freshness objective for the C5 gate~~ **Resolved (owner, 2026-11-10): 95% of source changes visible in Silver and Gold within 15 minutes** (ADR 0027).
15. ~~Locking mechanism~~ **Resolved (owner, 2026-11-10): a per-table lease via a DynamoDB conditional write**, in the same table as the checkpoints. To be implemented with the apply engine (C3).
16. ~~Where dbt runs~~ **Resolved: ADR 0029** (ECS Fargate task from `jobs/containers/dbt_runner`, started by Step Functions; runs as the data-engineer role).
17. ~~Networking~~ **Resolved: ADR 0027** (public RDS limited to the operator IP; DMS in private subnets with S3 gateway + Secrets Manager interface endpoints; no NAT).
18. ~~Bedrock model choice and ca-central-1 availability~~ **Resolved: ADR 0032** (Claude Haiku 5.5 through the Canada inference profile `ca.anthropic.claude-haiku-5-5`; Textract `DetectDocumentText` verified in ca-central-1). Model access for the account was still being granted on 2026-11-11.
19. **Human review queue:** where it lives and how approvals are recorded.
20. **BI / dashboard tool** for the tenant and admin dashboards.
21. **Snapshot retention period** for Iceberg `VACUUM`.
22. ~~Actual S3 bucket names~~ **Resolved: ADR 0019** (`procurelake-<env>-<role>-<account>-<region>`).
27. ~~Least-privilege deploy role~~ **Resolved: ADR 0036** (2026-11-11): `AdministratorAccess` replaced by `ReadOnlyAccess` plus a policy scoped to the `procurelake-dev` resources the four CI roots manage, with explicit denies (managed-policy attachment, identities, the CI roles, lake objects, the state key). Follow-up: permissions boundaries on workload roles.
28. **S3 Object Lock for Bronze.** Today immutability relies on versioning plus an IAM delete denial. Decide whether to adopt Object Lock, which must be enabled when a bucket is created, before any production use.
29. **Human AWS access.** Dev was deployed with IAM user `victoroko` (a long-lived access key plus temporary `AdministratorAccess`). Move to IAM Identity Center (SSO), remove the user's `AdministratorAccess`, and rotate or delete the access key.
30. ~~Re-collection policy for settled windows~~ **Resolved: ADR 0044** (owner, 2026-11-13): a weekly re-sweep (Sundays 04:00 Toronto) collects the 30 most recent settled days again; Silver keeps the latest `Last Modified Date` per award, a tie going to the later batch. Each run reports what the re-sweep found (new, changed, unchanged, older keys); drop or shorten it if it keeps finding nothing.

31. **Glue 6.0.** The AWS Pricing API lists Glue 6.0+ ETL at $0.308/DPU-hour in ca-central-1 against $0.44 for Glue 5.1 (checked 2026-11-10). Moving changes the Spark/Python/Iceberg versions fixed in ADR 0015, so the local test stack would change too. Decide before scaling the Silver jobs.
32. **Glue dependency source.** The Silver job installs exact pins from PyPI at every run start (ADR 0025). Decide whether to host the dependency wheels in the artifacts bucket (`.gluewheels.zip` with `--no-index`) to remove the runtime dependency on PyPI.
33. **Glue log encryption.** `/aws-glue/jobs/{error,output}` use CloudWatch default encryption. Decide whether to use a KMS key (needs a Glue security configuration and `logs:AssociateKmsKey`).
34. ~~USAspending in Silver~~ **Resolved: ADR 0044** (owner, 2026-11-13): `silver_usaspending_contract_awards`, one row per `generated_internal_id` at its latest `Last Modified Date`; upsert only (the API reports no deletes); amounts exact in USD.

35. **Canonical supplier.** `dim_supplier` is one row per supplier registration within a tenant. Decide whether, and how, suppliers are matched across tenants or names (an entity-resolution policy) before any cross-tenant supplier reporting.
36. **`gold_staging` database.** Staging is ephemeral (ADR 0026), so `procurelake_dev_gold_staging` is empty. Keep it for materialized intermediate models, or remove it (and its IAM grants) in the next foundation change.

37. **RDS certificate verification.** The operator scripts connect with `sslmode=require` and do not verify the server certificate against the RDS CA bundle (ADR 0027). Decide whether to pin the bundle (`verify-full`).
38. **Tenant catalog visibility.** Tenant roles can read Glue metadata (table names and schemas, not rows) for every Gold table (ADR 0030). Decide whether to limit `glue:GetTable(s)` to the tenant-facing tables, or accept schema visibility.
39. **DMS and freshness alarm tuning (Week 8 drill).** Three findings, measured but not explained. (a) The DMS latency alarm (5 of 5 one-minute periods, missing data breaching) went to ALARM 11.8 min after the task stopped, where about 5 min was expected; the EventBridge "task stopped" event arrived within a minute. (b) The same alarm fires once when the stack is created, before the task publishes metrics. (c) The freshness alarm went to ALARM at 23:55:17Z and cleared by itself at 00:10:17Z with no newer batch (last datapoint 23:54Z), so it stays in ALARM about 15 minutes after a late batch whether or not anything changes; an OK email therefore does not mean a later batch was on time. Decide whether to alarm on the stopped event, suppress the startup alarm, and whether the freshness alarm's clear needs to mean more than "time passed".
40. **CDC freshness under the 5-minute schedule (Week 10 benchmark).** A `cdc_processing` run takes about 7.5 min (apply ~2.5, dbt ~5), longer than the 5-minute rate, so alternate ticks are skipped and the worst case from commit to Gold is about 17.5 min at any volume. Measured 2026-11-12: 599 changes met the 15-minute gate (Gold p95 736 s); 5,929 changes committed over 6 min missed it (latest commit 15.7 min to Gold; harness p95 1,338 s). Decide: shorten dbt (build only models downstream of changed tables), start each run when the previous ends instead of on a fixed rate, or relax the gate (`docs/benchmarks/2026-11-12-replay-concurrency-cdc.md` §3).
41. **Document intelligence state machine.** Deferred by the owner on 2026-11-12 until the Bedrock quota for Claude Haiku 5.5 is granted, so it is built and verified live with extraction (ADR 0032, 0040). Decide its trigger then: on landing of new documents, or a schedule.

## Naming inconsistencies between the source documents
23. ~~Cycle-time mart name~~ **Resolved: ADR 0017** (`mart_procurement_cycle_time`).
24. ~~Bronze Postgres prefix~~ **Resolved: ADR 0017** (`bronze/postgres/`). How full-load and CDC output are laid out is deferred to ADR 0014 (Week 5).
25. ~~Terraform layout~~ **Resolved: ADR 0017** (`infrastructure/live/`).
26. ~~Package root~~ **Resolved: ADR 0017** (`src/procurelake/`).
