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
| 0013 | DMS → S3 → Iceberg CDC vs. AWS Zero-ETL (evaluation) | Proposed | not written (Week 10) |
| 0014 | DMS Parquet output and its ordering limits vs. transaction-preserving CSV/streaming | Proposed | not written (Week 5) |
| 0015 | Runtime versions: Glue 5.1 / Python 3.11, PostgreSQL 17 | Accepted | [0015-runtime-versions.md](0015-runtime-versions.md) |
| 0016 | Operational (OLTP) schema design | Accepted | [0016-oltp-schema-design.md](0016-oltp-schema-design.md) |
| 0017 | Naming conflicts between planning documents | Accepted | [0017-naming-conventions.md](0017-naming-conventions.md) |
| 0018 | Deterministic synthetic data generator | Accepted | [0018-synthetic-data-generator.md](0018-synthetic-data-generator.md) |

Update the **ADR file** column as each ADR is written.
