# ProcureLake

**Enterprise Procurement Intelligence & AI Data Platform.** A unified, production-style AWS procurement lakehouse built as a portfolio-grade reference implementation.

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

## Status (2026-11-14)

Only `dev` is deployed (`ca-central-1`); `staging` and `prod` are not. Each line links to the record of what was run; [`docs/implementation-plan.md`](docs/implementation-plan.md) has the full week-by-week status.

| Capability | State in dev | Evidence |
|---|---|---|
| Lakehouse: Bronze, Silver Iceberg, Gold dbt | Live. Operational tables in Silver; 19 dbt models with their tests on Athena; Iceberg compaction and retention run daily | [ADR 0024](docs/adr/0024-silver-framework.md), [ADR 0025](docs/adr/0025-silver-glue-job.md), [ADR 0026](docs/adr/0026-dbt-gold.md), [ADR 0033](docs/adr/0033-iceberg-maintenance.md), [Week 3](docs/evidence/sprint1-week3-silver-live.md), [Week 4](docs/evidence/sprint1-week4-gold-live.md) |
| Public reference data | USAspending contracts in Silver (51,712 awards from 6 settled days, as of 2026-11-14); Government of Canada contracts in Bronze (1,336,184 records). Kept separate from tenant data and readable by the admin product only | [ADR 0020](docs/adr/0020-ingestion-framework.md), [ADR 0039](docs/adr/0039-csv-snapshot-connectors.md), [ADR 0044](docs/adr/0044-usaspending-silver-and-resweep.md), [ADR 0046](docs/adr/0046-usaspending-keyset-paging.md), [ADR 0047](docs/adr/0047-data-and-platform-policy-decisions.md) |
| CDC | Live during CDC sessions (the `dev/cdc` stack is disposable). Measured 2026-11-10: p95 204 s from commit to Gold for 47 changes, and 459 s on the 5-minute schedule. At 5,929 changes in 6 minutes the 15-minute gate was missed; selective dbt builds were deployed 2026-11-14 to address it and are **not yet measured** | [Week 5](docs/evidence/sprint1-week5-cdc-freshness-live.md), [Week 6](docs/evidence/sprint1-week6-orchestration-live.md), [benchmark](docs/benchmarks/2026-11-12-replay-concurrency-cdc.md), [ADR 0050](docs/adr/0050-cdc-hardening.md) |
| Multi-tenant analytics | Live. Lake Formation row filters on four marts, per-tenant Athena workgroups, daily tenant exports, an admin cross-tenant product; tenants can see only their own marts in the Glue catalog. `make test-aws`: 30 passed on 2026-11-14 | [ADR 0030](docs/adr/0030-tenant-isolation-lake-formation.md), [ADR 0049](docs/adr/0049-catalog-tightening.md), [Week 7](docs/evidence/sprint1-week7-tenant-products-live.md) |
| Orchestration | Live. Step Functions for CDC processing, dbt, batch ingestion, data quality, tenant exports and document intelligence; EventBridge schedules; dbt and task runners on ECS Fargate | [ADR 0029](docs/adr/0029-orchestration-step-functions.md), [ADR 0040](docs/adr/0040-batch-ingestion-and-data-quality-orchestration.md), [ADR 0045](docs/adr/0045-document-intelligence-orchestration.md) |
| Reliability & governance | Live. CloudWatch alarms and dashboard, a DMS stall drill detected and recovered, owners and classifications on every Silver and Gold table (`make catalog-check`), source-to-Gold lineage (`make lineage`) | [ADR 0031](docs/adr/0031-monitoring-and-alerting.md), [ADR 0042](docs/adr/0042-catalog-governance-and-lineage.md), [incident](docs/incidents/2026-11-10-cdc-dms-stall-drill.md) |
| AI procurement intelligence | **Blocked.** Extraction, validation, the human review queue and the document state machine are built and deployed, but the account's Bedrock quota for Claude Haiku 5.5 is 0, so no live extraction has run | [ADR 0032](docs/adr/0032-ai-contract-extraction.md), [ADR 0045](docs/adr/0045-document-intelligence-orchestration.md) |
| Acceptance tests | All 12 tests of the plan's acceptance suite pass (local and live tiers) | [ADR 0041](docs/adr/0041-acceptance-test-suite.md) |

**Not built:** the tenant/admin BI dashboard (deferred by owner decision), synthetic application events (an optional extension), and the recorded demo (script ready in [`docs/runbooks/demo.md`](docs/runbooks/demo.md)). Open decisions are tracked in [`docs/open-questions.md`](docs/open-questions.md).

Figures above are measured results from dev. SLO targets in this repo are design targets to validate, not achieved results.

## Architecture

What dev (`ca-central-1`) runs: the CDC, batch, Gold and document paths, numbered, with the legend at the bottom of the image. dbt runs as the dbt runner (dbt-athena on ECS Fargate), started by Step Functions, and builds Gold through Athena. Drawn with [TerraVision](https://github.com/patrickchugh/terravision) from a curated graph of real dev resources and regenerated with `make diagrams`.

<p align="center">
  <a href="docs/architecture/diagrams/dev-platform.svg">
    <img src="docs/architecture/diagrams/dev-platform.png" alt="ProcureLake dev architecture: RDS PostgreSQL to DMS to S3 Bronze, Glue jobs to Silver Iceberg, dbt on ECS Fargate and Athena to Gold, Lake Formation to tenant workgroups, and contract documents through Textract and Bedrock to a review queue" width="560">
  </a>
</p>

The diagram was last updated on 2026-11-14. `dev/cdc` (RDS, DMS, VPC) is disposable and runs only during CDC sessions. [`docs/architecture/overview.md`](docs/architecture/overview.md) describes the full architecture, and [`docs/architecture/diagrams/`](docs/architecture/diagrams/README.md) has per-stack diagrams and notes on how the combined one is built.

## Core stack
AWS (`ca-central-1`) · S3 · Apache Iceberg · AWS Glue (Spark + Data Catalog) · Amazon Athena (engine v3) · dbt (`dbt-athena`) · AWS DMS · RDS PostgreSQL · Step Functions · EventBridge · DynamoDB · ECS Fargate · Lake Formation · IAM · KMS · Secrets Manager · CloudWatch · SNS · Textract · Bedrock · Terraform · GitHub Actions (OIDC) · Python (uv, Ruff, pytest, Pydantic) · Docker.

## Data sources
| Source | Ingestion | State |
|---|---|---|
| Synthetic PostgreSQL OLTP (two fictional tenants) | AWS DMS full load + CDC; snapshots to Bronze | Live |
| USAspending API | Python API ingestion, daily, with a weekly re-sweep | Live in Silver |
| Government of Canada Open Data procurement contracts | CSV snapshot ingestion | Live in Bronze |
| Public US federal contract PDFs (public domain) | S3 + Textract + Bedrock, human review | Built; blocked by the Bedrock quota |
| Synthetic application events | Python events (streaming is an optional extension) | Not built |

Synthetic tenants are clearly labeled and are **not** real SOVRA customers. Public data keeps its original provenance.

## Getting started (local)
Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), make. The Spark tests also need Java 17.

```bash
cp .env.example .env      # then replace the placeholder password (local only)
make install              # uv sync --locked (Python 3.11)
make db-up                # PostgreSQL 17 with wal_level=logical
make db-migrate           # creates the `procurement` schema
make db-seed              # loads the synthetic two-tenant dataset (seed 42)
make lint
make test                 # unit, PostgreSQL integration and Spark tests
make test-acceptance      # acceptance tests, local tier
```

Run `make help` to list all targets. No AWS account is needed for any of these.

## Deploying to AWS
Infrastructure is Terraform in `infrastructure/`: reusable modules plus independent `dev` roots (`foundation`, `data_platform`, `security`, `observability`, and the disposable `cdc`). See [`infrastructure/README.md`](infrastructure/README.md) for the bootstrap and deployment guide.

GitHub Actions plans every root on pull requests into `dev`. On a push to `dev` it applies them after the owner approves the `dev` environment, and only if the plan still matches the one approved (ADR 0035). The bootstrap root (state bucket, budget, CI roles) is applied by hand.

## Documentation
| Document | What it covers |
|---|---|
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | Weekly plan, completion gates and their status |
| [`docs/adr/`](docs/adr/README.md) | Architecture decision records (0001–0050) |
| [`docs/open-questions.md`](docs/open-questions.md) | Decisions not yet made |
| [`docs/architecture/`](docs/architecture/overview.md) | Architecture overview, data model, diagrams |
| [`docs/operations/slos_and_observability.md`](docs/operations/slos_and_observability.md) | SLO targets and monitoring |
| [`docs/security/security-review.md`](docs/security/security-review.md) | Security review and residual risks |
| [`docs/runbooks/`](docs/runbooks/) | Alarm, CDC stall, Iceberg maintenance and demo runbooks |
| [`docs/evidence/`](docs/evidence/), [`docs/benchmarks/`](docs/benchmarks/), [`docs/incidents/`](docs/incidents/) | Records of live runs, benchmarks (including a cost review) and drills |
