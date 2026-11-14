# Open questions

These are decisions the planning documents **do not make**. Agents and contributors must not pick an answer silently. Resolve each one with the project owner, record the decision (as an ADR where appropriate), and then remove it from this list.

## Tooling and versions
1. ~~Python version and Glue runtime~~ **Resolved: ADR 0015** (Glue 5.1, Python 3.11, PostgreSQL 17).
2. ~~Pinned versions for dbt~~ **Resolved: ADR 0026** (dbt-core 1.12.6, dbt-athena 1.11.1; no dbt packages yet: pin any added package in `packages.yml` with an ADR).
3. ~~Iceberg integration in Glue~~ **Resolved: ADR 0025** (Glue 5.1, `--datalake-formats iceberg`, a Spark catalog `glue_catalog` with `org.apache.iceberg.aws.glue.GlueCatalog` and `S3FileIO`).
4. ~~Local Glue testing approach~~ **Resolved: ADR 0024** (local PySpark 3.5.8 (Glue 5.1 runs 3.5.6, same 3.5 line) + Iceberg 1.10.0 + Java 17; a live Glue run verifies the AWS side).
5. ~~Security scanning tools~~ **Resolved: ADR 0037** (owner, 2026-11-11): `ci-security.yml` runs Checkov 3.3.30 (Terraform), pip-audit 2.10.1 (`uv.lock`, hash-checked) and gitleaks v8.30.1 (full history, checksum-verified binary) on PRs, pushes and weekly. Waivers carry reasons: inline per resource, repository-wide in `.checkov.yaml`, by fingerprint in `.gitleaksignore`.
6. ~~Terraform remote state backend~~ **Resolved: ADR 0019** (S3 bucket, native `use_lockfile` locking).

## Data model
7. ~~Full OLTP column lists~~ **Resolved: ADR 0016.**
8. ~~Late-arrival policy~~ **Resolved: ADR 0043** (owner, 2026-11-13): ordering is resolved in Silver by source sequence (ADR 0028); Gold has no lookback window because every model reads the full current Silver state each run. Revisit when a full recompute no longer fits the CDC schedule.
9. ~~Delete semantics in Gold~~ **Resolved: ADR 0026** (hard removal; history via Iceberg snapshots).
10. ~~Exchange-rate source and conversion policy~~ **Resolved: ADR 0047** (no conversion: per-currency reporting only; a cross-currency report needs a new ADR naming a rate source).
11. ~~Supplier canonicalization rules~~ **Resolved: ADR 0047** (no matching across tenants or names; merged with 35).
12. ~~How public reference data relates to tenant data~~ **Resolved: ADR 0047** (kept separate, admin product only; never joined into tenant marts).
13. ~~Partitioning strategy for each Silver and Gold Iceberg table~~ **Resolved: ADR 0034** (owner, 2026-11-11, on the 100k and 1M measurements): tenant-owned Silver tables stay partitioned by `tenant_id` only; Gold stays unpartitioned, as dbt builds it now. Add `months()` on `silver_invoices.invoice_date` and `silver_awards.awarded_at` (metadata change plus one rewrite, `make bench-partition-evolve` shows the mechanics) once a date-filtered Silver scan exceeds Athena's 10 MB per-query minimum. Measured: −91%/−97% bytes but no time or billable saving up to 1M rows; the largest Silver scan was 2.6 MB. Gold was measured at the same sizes, not tested with partitions.

## Platform
14. ~~Freshness objective for the C5 gate~~ **Resolved (owner, 2026-11-10): 95% of source changes visible in Silver and Gold within 15 minutes** (ADR 0027).
15. ~~Locking mechanism~~ **Resolved (owner, 2026-11-10): a per-table lease via a DynamoDB conditional write**, in the same table as the checkpoints. To be implemented with the apply engine (C3).
16. ~~Where dbt runs~~ **Resolved: ADR 0029** (ECS Fargate task from `jobs/containers/dbt_runner`, started by Step Functions; runs as the data-engineer role).
17. ~~Networking~~ **Resolved: ADR 0027** (public RDS limited to the operator IP; DMS in private subnets with S3 gateway + Secrets Manager interface endpoints; no NAT).
18. ~~Bedrock model choice and ca-central-1 availability~~ **Resolved: ADR 0032** (Claude Haiku 5.5 through the Canada inference profile `ca.anthropic.claude-haiku-5-5`; Textract `DetectDocumentText` verified in ca-central-1). Model access for the account was still being granted on 2026-11-11.
19. ~~Human review queue~~ **Resolved: ADR 0032** (a DynamoDB review queue with a command-line reviewer; every extraction is approved or rejected once by a named reviewer, `auto_approve` false).
20. **BI / dashboard tool** for the tenant and admin dashboards.
21. ~~Snapshot retention period~~ **Resolved: ADR 0033** (Silver 7 days with at least 10 snapshots; Gold marts 1 day with at least 5).
22. ~~Actual S3 bucket names~~ **Resolved: ADR 0019** (`procurelake-<env>-<role>-<account>-<region>`).
27. ~~Least-privilege deploy role~~ **Resolved: ADR 0036** (2026-11-11): `AdministratorAccess` replaced by `ReadOnlyAccess` plus a policy scoped to the `procurelake-dev` resources the four CI roots manage, with explicit denies (managed-policy attachment, identities, the CI roles, lake objects, the state key). Follow-up: permissions boundaries on workload roles.
28. ~~S3 Object Lock for Bronze~~ **Resolved: ADR 0047** (not in dev; a staging or production Bronze bucket is created with Object Lock in governance mode).
29. **Human AWS access.** Dev was deployed with IAM user `victoroko` (a long-lived access key plus temporary `AdministratorAccess`). Move to IAM Identity Center (SSO), remove the user's `AdministratorAccess`, and rotate or delete the access key.
30. ~~Re-collection policy for settled windows~~ **Resolved: ADR 0044** (owner, 2026-11-13): a weekly re-sweep (Sundays 04:00 Toronto) collects the 30 most recent settled days again; Silver keeps the latest `Last Modified Date` per award, a tie going to the later batch. Each run reports what the re-sweep found (new, changed, unchanged, older keys); drop or shorten it if it keeps finding nothing.

31. ~~Glue 6.0~~ **Resolved: ADR 0047** (stay on Glue 5.1; revisit before scaling the Silver jobs or at 5.1 end of support).
32. ~~Glue dependency source~~ **Resolved: ADR 0048** (dependency wheels hosted in the artifacts bucket, installed with `--no-index --no-deps`).
33. ~~Glue log encryption~~ **Resolved: ADR 0048** (a Glue security configuration encrypts job logs with the data key).
34. ~~USAspending in Silver~~ **Resolved: ADR 0044** (owner, 2026-11-13): `silver_usaspending_contract_awards`, one row per `generated_internal_id` at its latest `Last Modified Date`; upsert only (the API reports no deletes); amounts exact in USD.

35. ~~Canonical supplier~~ **Resolved: ADR 0047** (one row per supplier registration within a tenant; no cross-tenant matching (with 11)).
36. ~~`gold_staging` database~~ **Resolved: ADR 0049** (removed).

37. ~~RDS certificate verification~~ **Resolved: ADR 0050** (`sslmode=verify-full` with the pinned RDS CA bundle).
38. ~~Tenant catalog visibility~~ **Resolved: ADR 0049** (tenant roles read Glue metadata of their tenant-facing marts only).
39. ~~DMS and freshness alarm tuning~~ **Resolved: ADR 0050** (a DMS task-stopped alert, the latency alarm treats missing data as missing, the freshness alarm's OK is documented).
40. ~~CDC freshness under the 5-minute schedule~~ **Resolved: ADR 0050** (selective dbt builds of the queued tables plus a daily full build; the latency gain is not yet measured).
41. ~~Document intelligence state machine~~ **Resolved: ADR 0045** (owner, 2026-11-14): built now, started when a document's landing manifest is written (EventBridge on the artifacts bucket), one execution per document; extraction runs in the task runner as the AI extraction role and stops at the review queue. Live runs wait on the Bedrock quota (0 tokens/min applied; an AWS Support case is needed).

## Naming inconsistencies between the source documents
23. ~~Cycle-time mart name~~ **Resolved: ADR 0017** (`mart_procurement_cycle_time`).
24. ~~Bronze Postgres prefix~~ **Resolved: ADR 0017** (`bronze/postgres/`). How full-load and CDC output are laid out is deferred to ADR 0014 (Week 5).
25. ~~Terraform layout~~ **Resolved: ADR 0017** (`infrastructure/live/`).
26. ~~Package root~~ **Resolved: ADR 0017** (`src/procurelake/`).
