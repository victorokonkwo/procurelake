# ProcureLake

**Enterprise Procurement Intelligence & AI Data Platform.** A unified, production-style AWS procurement lakehouse built as a portfolio-grade reference implementation.

> **Status:** Sprint 1, Days 1–4 complete: local PostgreSQL operational schema and synthetic two-tenant data, the AWS dev foundation (Terraform: budget, KMS, S3 lakehouse buckets, IAM, GitHub OIDC) deployed in `ca-central-1`, the first live public-data ingestion (USAspending → Bronze, with reconciled manifests), and Bronze queryable in Athena (Glue catalog, encrypted engineering workgroup, live integration tests). The Sprint 1 plan and foundation checklist are complete: CI (GitHub Actions), pre-commit hooks, and schema-checked source contracts. Week 3: the operational tables are snapshotted into Bronze and built into Silver Apache Iceberg tables by an AWS Glue 5.1 job, queryable in Athena (`docs/evidence/sprint1-week3-silver-live.md`). Week 4: the first full Bronze-to-Gold run, with dbt on Athena building Iceberg dimensions, facts and marts, 49 passing data tests, and a Gold metric traced back to its source row (`docs/evidence/sprint1-week4-gold-live.md`). See `docs/implementation-plan.md`.

## What it does

A fictional procurement SaaS company serves multiple government agencies. Procurement data arrives from transactional databases, public datasets, and contract documents. ProcureLake brings it together as one governed platform with five capabilities:

1. **Enterprise Lakehouse**: Bronze/Silver/Gold on S3 and Apache Iceberg
2. **CDC Processing**: PostgreSQL → AWS DMS → S3 → Iceberg
3. **Multi-Tenant Analytics**: isolated analytics for two synthetic tenants (`tenant_alder`, `tenant_birch`)
4. **Reliability & Governance**: dbt/Glue quality checks, CloudWatch monitoring, lineage, incident recovery
5. **AI Procurement Intelligence**: Textract + Amazon Bedrock contract extraction with validation and human review

### Business questions answered
- How much did each agency award, by supplier and category? (Awarded value and actual spend are separate metrics.)
- Which suppliers won the most contracts?
- What is the average procurement cycle time?
- How quickly do source-system changes become available for analytics?
- Can a tenant access only its own authorized records?
- Can contract documents be turned into validated, AI-ready structured data?

## Core stack
AWS (`ca-central-1`) · S3 · Apache Iceberg · AWS Glue (Spark + Data Catalog) · Amazon Athena (engine v3) · dbt (`dbt-athena`) · AWS DMS · RDS PostgreSQL · Step Functions · EventBridge · DynamoDB · Lake Formation · IAM · KMS · Secrets Manager · CloudWatch · SNS · Textract · Bedrock · Terraform · GitHub Actions (OIDC) · Python (uv, Ruff, pytest, Pydantic) · Docker.

## Data sources
| Source | Ingestion |
|---|---|
| Synthetic PostgreSQL OLTP (two fictional tenants) | AWS DMS full load + CDC |
| USAspending API | Python API ingestion |
| Government of Canada Open Data procurement contracts | Scheduled CSV ingestion |
| Public contract PDFs | S3 + Textract + Bedrock |
| Synthetic application events | Python events (streaming is an optional extension) |

Synthetic tenants are clearly labeled and are **not** real SOVRA customers.

## Getting started (local)
Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), make.

```bash
cp .env.example .env      # then replace the placeholder password (local only)
make install              # uv sync --locked (Python 3.11)
make db-up                # PostgreSQL 17 with wal_level=logical
make db-migrate           # creates the `procurement` schema
make db-seed              # loads the synthetic two-tenant dataset (seed 42)
make lint
make test                 # unit + PostgreSQL integration tests
```

Run `make help` to list all targets.
