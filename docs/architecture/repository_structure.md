# Repository structure (target)

ProcureLake is a **domain-oriented monorepo**. Infrastructure, data engineering, transformation, orchestration, governance, AI, and deployment each have their own area, and each area has its own deployment boundary.

The tree below is the **target** layout. Don't create it all at once. Create directories when real content goes into them.

```
procurelake/
├── .github/
│   ├── workflows/
│   │   ├── ci-python.yml
│   │   ├── ci-dbt.yml
│   │   ├── ci-terraform.yml
│   │   ├── ci-security.yml
│   │   ├── deploy-dev.yml
│   │   ├── deploy-staging.yml
│   │   ├── deploy-prod.yml
│   │   └── release.yml
│   ├── CODEOWNERS
│   └── pull_request_template.md
│
├── infrastructure/
│   ├── bootstrap/                  # main.tf, variables.tf, outputs.tf (remote state backend)
│   ├── modules/
│   │   ├── networking/  s3_lakehouse/  kms/  iam/  secrets_manager/
│   │   ├── rds_postgresql/  dms/  glue_catalog/  glue_jobs/  athena/
│   │   ├── lake_formation/  dynamodb/  lambda/  step_functions/
│   │   └── eventbridge/  bedrock/  monitoring/  budgets/
│   ├── live/
│   │   ├── dev/      {foundation, data_platform, security, observability}/
│   │   ├── staging/  {foundation, data_platform, security, observability}/
│   │   └── prod/     {foundation, data_platform, security, observability}/
│   └── README.md
│
├── src/procurelake/
│   ├── __init__.py
│   ├── common/          logging.py, aws_clients.py, configuration.py, exceptions.py, metrics.py, retry.py
│   ├── ingestion/
│   │   ├── base_connector.py
│   │   ├── connectors/  usaspending.py, canada_contracts.py, csv_connector.py, postgres_snapshot.py
│   │   ├── extractors/
│   │   ├── validators/
│   │   ├── manifest.py
│   │   └── landing.py
│   ├── cdc/             event_parser.py, ordering.py, deduplication.py, iceberg_merge.py,
│   │                    checkpoints.py, reconciliation.py, replay.py
│   ├── lakehouse/
│   │   ├── bronze/      ingestion_audit.py, file_validation.py
│   │   ├── silver/      normalize_agencies.py, normalize_suppliers.py, normalize_awards.py, normalize_invoices.py
│   │   ├── iceberg/     table_manager.py, schema_evolution.py, compaction.py, maintenance.py
│   │   └── quality/     validations.py, quarantine.py
│   ├── ai/              document_ingestion.py, text_extraction.py, bedrock_extraction.py,
│   │                    schema_validation.py, evaluation.py, human_review.py, provenance.py
│   └── data_products/   exports/, access_control/, publishing/
│
├── jobs/
│   ├── glue/            bronze_to_silver.py, apply_cdc.py, reconcile_awards.py,
│   │                    process_documents.py, maintain_iceberg.py
│   ├── lambda/          ingestion_trigger/, pipeline_monitor/, export_trigger/
│   └── containers/      api_ingestion/, dbt_runner/, data_generator/
│
├── dbt/
│   ├── dbt_project.yml
│   ├── packages.yml
│   ├── models/
│   │   ├── staging/         postgres/, usaspending/, canada_contracts/
│   │   ├── intermediate/    procurement/, finance/
│   │   ├── core/
│   │   │   ├── dimensions/  dim_supplier.sql, dim_agency.sql, dim_date.sql
│   │   │   └── facts/       fct_contract_award.sql, fct_purchase_order.sql, fct_invoice.sql
│   │   └── marts/
│   │       ├── procurement/       mart_awarded_value.sql, mart_supplier_concentration.sql, mart_procurement_cycle_time.sql
│   │       ├── finance/           mart_actual_spend.sql
│   │       ├── tenant_analytics/
│   │       └── ai_intelligence/
│   ├── macros/  snapshots/  seeds/  analyses/
│   └── tests/   generic/, singular/
│
├── orchestration/
│   ├── step_functions/  batch_ingestion.asl.json, cdc_processing.asl.json, dbt_transformation.asl.json,
│   │                    data_quality.asl.json, document_intelligence.asl.json, tenant_exports.asl.json
│   ├── eventbridge/     schedules.yaml
│   └── README.md
│
├── database/postgres/   migrations/, schema/, seed/, replication/, tests/
│
├── contracts/
│   ├── source/          awards.yaml, suppliers.yaml, agencies.yaml, invoices.yaml
│   ├── products/        supplier_spend.yaml, procurement_performance.yaml, contract_intelligence.yaml
│   └── schemas/         json/, avro/
│
├── configs/
│   ├── environments/    dev.yaml, staging.yaml, prod.yaml
│   ├── sources/  pipelines/  quality_rules/  tenants/
│   └── ai/              prompts/, model_config.yaml, extraction_schema.json
│
├── governance/          classifications/, data_ownership/, retention/, access_policies/, lineage/, audit/
├── observability/       dashboards/, alarms/, slo/, reconciliation/, incident_templates/
├── analytics/           athena_queries/, dashboards/, sample_reports/
│
├── tests/
│   ├── unit/            ingestion/, cdc/, lakehouse/, ai/, data_products/
│   ├── integration/     aws/, glue/, iceberg/, dbt/
│   ├── e2e/             test_full_ingestion.py, test_cdc_to_gold.py,
│   │                    test_ai_document_to_gold.py, test_pipeline_recovery.py
│   ├── security/        test_tenant_isolation.py, test_unauthorized_access.py
│   ├── performance/  contract/  fixtures/
│
├── scripts/             bootstrap/, deployment/, backfill/, maintenance/, local/, security/
├── docker/              docker-compose.yml, Dockerfile.glue-tests, Dockerfile.python, Dockerfile.dbt
├── docs/
│   ├── architecture/    system_context.md, logical_architecture.md, physical_architecture.md,
│   │                    data_flow.md, security_architecture.md
│   ├── adr/  diagrams/  data_dictionary/  deployment/  runbooks/
│   └── disaster_recovery/  cost_optimization/  benchmarks/  interview_demo/
│
├── .gitignore  .pre-commit-config.yaml  .python-version  .env.example
├── pyproject.toml  uv.lock  Makefile
├── CODE_OF_CONDUCT.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
└── README.md
```

`docs/architecture/` currently holds `overview.md`, `data_model.md`, and this file. Split them into the five architecture documents named above once there's enough content to justify it.

## Capability-to-folder map

| Capability | Primary folders |
|---|---|
| Enterprise AWS Lakehouse | `src/procurelake/lakehouse/`, `dbt/`, `infrastructure/` |
| CDC & incremental processing | `src/procurelake/cdc/`, `database/postgres/replication/`, `jobs/glue/` |
| Multi-tenant data products | `src/procurelake/data_products/`, `governance/`, `dbt/models/marts/tenant_analytics/` |
| Reliability & governance | `observability/`, `contracts/`, `tests/`, `governance/` |
| AI procurement intelligence | `src/procurelake/ai/`, `configs/ai/`, `dbt/models/marts/ai_intelligence/` |

## Boundary rules

1. **Business logic is separate from execution infrastructure.** Reusable logic lives in `src/procurelake/`. Files in `jobs/` are thin entrypoints. For example, `jobs/glue/apply_cdc.py` invokes `src/procurelake/cdc/iceberg_merge.py`.
2. **Terraform defines and deploys AWS resources only.** It never contains Python transformations or business SQL.
3. `infrastructure/modules/` holds reusable building blocks. Each `infrastructure/live/<env>/<area>/` is an independent root module with its own remote state.

## Engineering standards

- **Security:** no secrets in Git. Use Secrets Manager, least-privilege IAM, GitHub OIDC, encryption, and automated security scanning.
- **Reproducibility:** pin Python dependencies, dbt packages, Glue runtime dependencies, and Terraform provider versions. Build versioned Python wheels for deployment.
- **Testing:** unit tests on every PR. AWS integration tests in dev. End-to-end and tenant-security tests before promotion.
- **Data contracts:** version source schemas and Gold product definitions, including grain, ownership, freshness, and compatibility rules.
- **Operational readiness:** every pipeline has structured logging, retry behavior, alarms, a documented recovery procedure, and explicit success criteria.
- **Data safety:** use synthetic customer records. Never commit credentials, sensitive procurement documents, full raw extracts, Terraform state, or generated dbt targets.
- **Deployment safety:** independent Terraform state, protected environments, reviewed infrastructure plans, and manual approval for production deploys.
