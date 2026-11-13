# Architecture decision records

Each decision gets one file: `NNNN-short-title.md`, with the sections **Status**, **Context**, **Decision**, **Consequences**, and **Alternatives considered**.

ADRs 0001–0012 record decisions the planning documents already made. ADRs 0015–0017 were decided in Sprint 1, Day 1. **Verify the AWS service behavior each ADR depends on against current AWS documentation** when its component is implemented.

| # | Decision | Status | ADR file |
|---|---|---|---|
| 0001 | Primary region `ca-central-1` | Accepted | [0001-primary-region-ca-central-1.md](0001-primary-region-ca-central-1.md) |
| 0002 | Immutable, versioned S3 raw landing (Bronze) | Accepted | [0002-immutable-bronze-landing.md](0002-immutable-bronze-landing.md) |
| 0003 | Apache Iceberg on S3 for Silver/Gold | Accepted | [0003-apache-iceberg-table-format.md](0003-apache-iceberg-table-format.md) |
| 0004 | AWS Glue Data Catalog as the shared catalog | Accepted | [0004-glue-data-catalog.md](0004-glue-data-catalog.md) |
| 0005 | Glue Spark for Silver; dbt-athena (engine v3) for Gold | Accepted | [0005-glue-spark-silver-dbt-athena-gold.md](0005-glue-spark-silver-dbt-athena-gold.md) |
| 0006 | Step Functions + EventBridge orchestration | Accepted | [0006-step-functions-eventbridge-orchestration.md](0006-step-functions-eventbridge-orchestration.md) |
| 0007 | DynamoDB for checkpoints and run metadata | Accepted | [0007-dynamodb-operational-state.md](0007-dynamodb-operational-state.md) |
| 0008 | IAM + Lake Formation for tenant isolation, plus S3 path denial | Accepted | [0008-tenant-isolation-iam-lake-formation.md](0008-tenant-isolation-iam-lake-formation.md) |
| 0009 | Textract + Bedrock for document intelligence | Accepted | [0009-textract-bedrock-document-intelligence.md](0009-textract-bedrock-document-intelligence.md) |
| 0010 | GitHub Actions (OIDC) + Terraform; independent root modules per env/area | Accepted | [0010-cicd-github-actions-oidc-terraform.md](0010-cicd-github-actions-oidc-terraform.md) |
| 0011 | Single-account `dev` first; multi-account as the evolution path | Accepted | [0011-single-account-dev-first.md](0011-single-account-dev-first.md) |
| 0012 | Domain-oriented monorepo; business logic separated from job entrypoints | Accepted | [0012-monorepo-logic-separated-from-entrypoints.md](0012-monorepo-logic-separated-from-entrypoints.md) |
| 0013 | DMS → S3 → Iceberg CDC vs. AWS Zero-ETL (evaluation): DMS stays; Zero-ETL from RDS PostgreSQL targets Redshift only | Accepted (documentation review, nothing deployed) | [0013-cdc-dms-vs-zero-etl.md](0013-cdc-dms-vs-zero-etl.md) |
| 0014 | DMS Parquet output and its ordering limits vs. transaction-preserving CSV/streaming | Proposed | not written (Week 5) |
| 0015 | Runtime versions: Glue 5.1 / Python 3.11, PostgreSQL 17 | Accepted | [0015-runtime-versions.md](0015-runtime-versions.md) |
| 0016 | Operational (OLTP) schema design | Accepted | [0016-oltp-schema-design.md](0016-oltp-schema-design.md) |
| 0017 | Naming conflicts between planning documents | Accepted | [0017-naming-conventions.md](0017-naming-conventions.md) |
| 0018 | Deterministic synthetic data generator | Accepted | [0018-synthetic-data-generator.md](0018-synthetic-data-generator.md) |
| 0019 | Terraform state, bootstrap stack, CI authentication | Accepted | [0019-terraform-state-and-ci-authentication.md](0019-terraform-state-and-ci-authentication.md) |
| 0020 | Ingestion framework and USAspending connector | Accepted | [0020-ingestion-framework.md](0020-ingestion-framework.md) |
| 0021 | Bronze catalog tables and engineering Athena workgroup | Accepted | [0021-bronze-catalog-and-athena-workgroup.md](0021-bronze-catalog-and-athena-workgroup.md) |
| 0022 | CI, pre-commit hooks, and source contracts | Accepted | [0022-ci-pre-commit-and-source-contracts.md](0022-ci-pre-commit-and-source-contracts.md) |
| 0023 | Consistent PostgreSQL snapshot into Bronze | Accepted | [0023-postgres-snapshot-to-bronze.md](0023-postgres-snapshot-to-bronze.md) |
| 0024 | Silver framework: validation, quarantine, Iceberg snapshot apply | Accepted | [0024-silver-framework.md](0024-silver-framework.md) |
| 0025 | Silver Glue job: packaging, deployment, permissions | Accepted | [0025-silver-glue-job.md](0025-silver-glue-job.md) |
| 0026 | dbt Gold on Athena: versions, materialization, deletes, runner | Accepted | [0026-dbt-gold.md](0026-dbt-gold.md) |
| 0027 | CDC source (RDS PostgreSQL) and AWS DMS into Bronze | Accepted | [0027-cdc-source-and-dms.md](0027-cdc-source-and-dms.md) |
| 0028 | CDC apply engine: ordering, tombstones, checkpoint and lease | Accepted | [0028-cdc-apply-engine.md](0028-cdc-apply-engine.md) |
| 0029 | Orchestration: Step Functions, EventBridge Scheduler, ECS Fargate dbt runner | Accepted | [0029-orchestration-step-functions.md](0029-orchestration-step-functions.md) |
| 0030 | Tenant isolation: Lake Formation hybrid mode on Gold; marts merged, never dropped (amends ADR 0026 for marts) | Accepted | [0030-tenant-isolation-lake-formation.md](0030-tenant-isolation-lake-formation.md) |
| 0031 | Monitoring: CDC metrics, CloudWatch alarms and EventBridge to SNS, silent-stall drill | Accepted | [0031-monitoring-and-alerting.md](0031-monitoring-and-alerting.md) |
| 0032 | AI contract extraction: Claude Haiku 5.5 in Canada, evidence checks, human review | Accepted (live run blocked on model access) | [0032-ai-contract-extraction.md](0032-ai-contract-extraction.md) |
| 0033 | Iceberg maintenance: compaction after each dbt merge, daily OPTIMIZE/VACUUM job with layer retention | Accepted | [0033-iceberg-maintenance.md](0033-iceberg-maintenance.md) |
| 0034 | Benchmarks in an isolated namespace (`postgres_bench`, bench Silver/Gold); partition evolution in Spark; CDC lineage columns on every Silver table | Accepted (resolves OQ13) | [0034-benchmarks-and-partition-evolution.md](0034-benchmarks-and-partition-evolution.md) |
| 0035 | CI deploys: plan on pull requests, owner-approved apply on dev, fingerprint-checked (GitHub Actions + OIDC) | Accepted (verified end to end) | [0035-ci-deploys-github-actions.md](0035-ci-deploys-github-actions.md) |
| 0036 | Scoped CI deploy role: ReadOnlyAccess plus procurelake-dev writes, explicit denies (replaces AdministratorAccess) | Accepted (applied; used by CI) | [0036-scoped-ci-deploy-role.md](0036-scoped-ci-deploy-role.md) |
| 0037 | Security scanners in CI (Checkov, pip-audit, gitleaks) and the hardening they prompted (role trusts, default SGs, dbt task policy) | Accepted | [0037-security-scanning-and-hardening.md](0037-security-scanning-and-hardening.md) |
| 0038 | `make destroy-dev`: targeted, plan-verified teardown of the compute roots; keeps state, foundation, DynamoDB, ECR, logs | Accepted (dry run verified; not executed) | [0038-destroy-dev.md](0038-destroy-dev.md) |
| 0039 | CSV snapshot connectors: generic CSV and Government of Canada contracts over $10,000; byte-for-byte, header/field-count/key reconciliation, SHA-256 pinning, create-only multipart landing | Accepted (in dev Bronze 2026-11-12) | [0039-csv-snapshot-connectors.md](0039-csv-snapshot-connectors.md) |
| 0040 | Batch ingestion and daily data-quality state machines: task runner on Fargate (ingestion role; data engineer for the Silver checks), allow-listed tasks, schedules, alerts | Accepted (deployed and run live 2026-11-12) | [0040-batch-ingestion-and-data-quality-orchestration.md](0040-batch-ingestion-and-data-quality-orchestration.md) |
| 0041 | End-to-end acceptance suite (plan §3): local tier (PostgreSQL + Spark + test doubles) and live tier; Silver schema evolution for nullable contract additions | Accepted (local passing; live 2–3 and 8 wait on a CDC session) | [0041-acceptance-test-suite.md](0041-acceptance-test-suite.md) |
| 0042 | Catalog governance: owners and classifications in Glue (Silver from the contracts, version-checked; Gold via dbt `persist_docs`), `make catalog-check`, source-to-Gold lineage (`make lineage`) | Accepted (offline; not yet deployed) | [0042-catalog-governance-and-lineage.md](0042-catalog-governance-and-lineage.md) |

Update the **ADR file** column as each ADR is written.
