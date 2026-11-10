# Architecture overview

## 1. End-to-end flow

This is the target architecture. For a diagram of what is deployed in dev today, drawn from the Terraform with TerraVision, see [`diagrams/`](diagrams/README.md) (`diagrams/dev-platform.png`).

```
SOURCE SYSTEMS
  PostgreSQL (transactions)      Public APIs / CSV (contracts)      Documents (PDFs)
        │ AWS DMS                       │ Python / Glue                   │ Textract
        ▼                               ▼                                 ▼
BRONZE: S3 raw landing (immutable, versioned)
  Source files · CDC events · raw documents · ingestion metadata
        │ AWS Glue Spark + Bedrock processing
        │ validation · CDC apply · cleansing · structured extraction
        ▼
SILVER: Apache Iceberg (Glue Data Catalog)
  Clean suppliers · agencies · solicitations · awards · invoices · contract intelligence
        │ dbt + Amazon Athena
        │ incremental models · business rules · quality tests
        ▼
GOLD: Governed data products
  Supplier/award KPIs · procurement KPIs · tenant datasets · AI-ready data
        │
        ├── Analytics: Athena + BI
        ├── Customers: secure exports
        └── AI products: Bedrock

Cross-cutting: Lake Formation + IAM · CloudWatch + dbt tests · Step Functions · Terraform
```

## 2. Architectural decisions

| Area | Decision | Rationale |
|---|---|---|
| Primary region | `ca-central-1` | Default for deployment in a Canadian region |
| Raw landing | Immutable, versioned S3 objects | Replay and auditability |
| Silver storage | Apache Iceberg on S3 | ACID transactions, schema evolution, time travel |
| Catalog | AWS Glue Data Catalog | Metadata shared by Glue and Athena |
| Silver compute | AWS Glue Spark | CDC merge and complex cleansing |
| Gold compute | dbt on Athena (engine v3) | Modular, testable SQL models; Iceberg incremental `merge` |
| Orchestration | Step Functions + EventBridge | Scheduling, retries, state tracking |
| Operational state | DynamoDB | Pipeline checkpoints and run metadata |
| Access control | IAM + Lake Formation | Tenant-level authorization |
| AI | Textract + Bedrock | Document text extraction and structured classification |
| CI/CD | GitHub Actions (AWS OIDC) + Terraform | Repeatable, auditable deployment |
| Primary CDC path | DMS → S3 → Iceberg | Shows CDC handling and Silver transformation logic directly. Zero-ETL is evaluated in an ADR as an optional extension only |

The two main transformation patterns are **Glue Spark writing Iceberg** (Silver) and **dbt-athena Iceberg incremental `merge`** (Gold).

Each decision gets its own ADR in `docs/adr/`.

## 3. Environments

- **Target topology:** separate AWS accounts for dev, staging, and prod. Each has its own IAM, S3, Glue, Athena, and Terraform state.
- **Deployment:** GitHub monorepo (PRs, CI tests, approved releases) → GitHub Actions with AWS OIDC → scoped deployment roles per environment.
- Each environment has independent Terraform root modules (`foundation`, `data_platform`, `security`, `observability`), each with its own remote state and CI/CD deployment boundary. They instantiate the reusable modules in `infrastructure/modules/`.
- **Initial build:** one AWS account, `dev` only, with ingestion, engineering, and consumer identities separated by IAM roles. The staging and prod structure stays in place but isn't deployed.

## 4. S3 storage layout

The S3 object layout is separate from the repository folder layout. Bucket names below are illustrative; real names must be globally unique.

```
s3://procurelake-dev-raw/
├── bronze/
│   ├── postgres/{awards,suppliers,agencies,invoices,...}/
│   ├── usaspending/
│   ├── canada_contracts/
│   └── cdc/
├── documents/
└── quarantine/

s3://procurelake-dev-lake/
├── silver/{agencies,suppliers,awards,invoices,contract_intelligence,...}/
└── gold/{dimensions,facts,procurement_marts,finance_marts,ai_marts}/

s3://procurelake-dev-products/
├── tenant_alder/
├── tenant_birch/
└── published_exports/

s3://procurelake-dev-artifacts/
├── pipeline_manifests/
├── dbt_artifacts/
├── athena_results/
└── audit/
```

Rules:
- Partition raw deliveries by ingestion date and source metadata.
- For CDC, keep the source change metadata and the immutable raw files.
- Silver and Gold Iceberg tables manage their own layout. Never create folders by hand that bypass table metadata or partition specs.
- Tenant export buckets have explicit IAM and bucket policies. Tenant consumers never get direct access to shared lakehouse paths.
- Physically separate raw data, queryable Iceberg tables, and delivery artifacts.

## 5. Security architecture (summary)

- No secrets in Git. Use AWS Secrets Manager (for example, for RDS credentials).
- IAM least privilege. Separate roles for ingestion, engineering, consumers (one per tenant), and deployment (OIDC).
- KMS encryption for storage.
- Lake Formation row filters and column controls on Iceberg tables queried through Athena.
- Consumer roles are **explicitly denied** direct S3 access to shared lakehouse prefixes. Athena query-result locations are controlled per workgroup.
- Automated security scanning in CI.

## 6. Known technical constraints

- **DMS Parquet ordering:** DMS Parquet output on S3 is organized by table and doesn't preserve full cross-table transaction ordering. Ordering assumptions must be tested and documented, including when transaction-preserving CSV output or streaming would be needed.
- **Lake Formation scope:** Lake Formation governs access through integrated engines such as Athena. It doesn't secure other S3 access paths, so those must be restricted separately.
- **Iceberg snapshot expiry:** `VACUUM` removes expired snapshots, and they can no longer be used for time travel. Retention must be a deliberate choice.
- **Zero-ETL availability:** support varies by source engine and version, destination, and Region.
- **Budgets:** AWS Budget alerts are notifications, not hard spending caps.

Verify each constraint against current AWS documentation when you implement the component it affects.
