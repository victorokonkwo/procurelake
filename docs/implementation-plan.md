# ProcureLake: Implementation Plan

Source: the ProcureLake "Principal Data Engineer Implementation Plan" and "Production-Grade Repository Structure" design notes. This document restates them as an executable plan. Items those notes don't decide are listed in `docs/open-questions.md`, not filled in here.

**Planning assumptions**
- 10 weeks, part-time, about 15–25 focused engineering hours per week.
- Prior AWS, SQL, Python, and Terraform experience.
- One AWS account for `dev`. Ingestion, engineering, and consumer identities are separated logically with IAM roles. A multi-account dev/staging/prod topology is documented as the evolution path.
- Production-style reference implementation, not a highly available SaaS deployment.

**Critical milestone:** by the end of **Week 4** there must be a complete, working batch lakehouse (Bronze → Silver → Gold). Every later week extends and hardens that same architecture.

---

## 1. Roadmap summary

| Week | Focus | Key work | Deliverable |
|---|---|---|---|
| 1 | Architecture & domain design | Source schema, SLOs, ADRs, synthetic data generator, initial data contracts | Approved architecture; runnable local PostgreSQL |
| 2 | AWS infrastructure & Bronze | Terraform, S3, KMS, IAM, Glue Catalog, Athena, budgets, public-data ingestion | Reproducible AWS foundation and raw datasets |
| 3 | Silver Iceberg | Glue Spark jobs, cleansing, dedup, schema validation, table creation | Query-ready Silver Iceberg tables. **In progress:** step 1 done 2026-11-10: operational tables snapshotted into Bronze (ADR 0023). Step 2 done: Silver framework tested locally end to end on Spark 3.5.6 / Iceberg 1.10.0 (ADR 0024). Step 3 done: Glue 5.1 job deployed and run in dev (ADR 0025). **Gate met for the operational tables** (`docs/evidence/sprint1-week3-silver-live.md`). USAspending is not yet in Silver |
| 4 | Gold analytics & dbt | Dimensions, facts, marts, incremental models, tests, docs | **First full Bronze-to-Gold demonstration**. **Gate met 2026-11-10** (ADR 0026, `docs/evidence/sprint1-week4-gold-live.md`): 10 Gold models + 49 tests pass on dev Athena; a Gold metric traced to its Bronze line and source row. Incremental merge is deferred to Week 6 (owner decision) |
| 5 | Change data capture | RDS logical replication, DMS, S3 CDC landing, event metadata | Live insert/update/delete capture. **In progress:** C1 and C2 done 2026-11-10 (ADR 0027, `docs/evidence/sprint1-week5-cdc-bronze-live.md`): full load + CDC land in Bronze with commit timestamps; 41 of 41 changes matched. C3/C4 engine done and tested locally (ADR 0028: order-safe idempotent MERGE, tombstones, DynamoDB lease + checkpoint, crash-and-replay). Deployed and run on the Bronze CDC files: Silver and Gold reflect inserts, updates and deletes (`docs/evidence/sprint1-week5-cdc-silver-gold-live.md`). C5 freshness measured live 2026-11-10 (`docs/evidence/sprint1-week5-cdc-freshness-live.md`): 47 changes, p95 151 s to Silver and 204 s to Gold against the 15-minute gate, 0 mismatches, deletes verified. Next: a live retry (replay) run for the second C5 checkbox |
| 6 | CDC apply & orchestration | Idempotent merges, checkpoints, Step Functions, recovery workflows | End-to-end incremental pipeline. **In progress:** state machines, dbt runner (ECS Fargate) and 5-minute schedule deployed (ADR 0029); dbt state machine verified live, success and failure (`docs/evidence/sprint1-week6-orchestration-live.md`). **Gate met 2026-11-10:** the scheduled pipeline ran end to end (p95 339 s to Silver, 459 s to Gold), overlapping runs skip themselves, and the failure/recovery drill passed |
| 7 | Multi-tenant data products | Lake Formation policies, tenant roles, Athena workgroups, BI, exports | Two isolated tenant analytics experiences |
| 8 | Reliability & governance | CloudWatch, alarms, reconciliation, incident simulation, catalog, lineage | Operational dashboard and recovery evidence |
| 9 | AI procurement intelligence | Document processing, Bedrock extraction, validation, curated AI models | Working document-to-Gold pipeline |
| 10 | Optimization & release | Load tests, Iceberg maintenance, security checks, CI/CD, cost review, demo | Complete portfolio and interview demo. **In progress:** Iceberg compaction and retention live 2026-11-11 (ADR 0033, `docs/evidence/sprint1-week10-iceberg-maintenance-live.md`): dbt post-hook plus a daily Glue job; the bloated marts went from 47 data + 47 delete files to 1, live rows unchanged, tenant tests 27/27. Snapshot expiry by the scheduled job is still to be observed (first run 2026-11-12). Benchmarks at 100k done 2026-11-11 (ADR 0034, `docs/benchmarks/2026-11-11-bench-100k-1m.md`): isolated bench namespace; load measured (Silver 511 s, Gold 38.9 s); partition evolution in Spark; Athena pruning measured (−91% / −97% bytes on date-filtered queries, no time saved at this volume). 1M run done the same day (1,042,769 rows: Silver 3,242 s with invoices the bottleneck, Gold 41.5 s; pruning ratios held, still no time saved). Silver validation then fixed: the same 1M batch in 180 s instead of 3,242 s (18×), identical results. OQ13 resolved by the owner: `tenant_id`-only Silver, unpartitioned Gold, months() when date-filtered scans pass 10 MB (ADR 0034). CI deploys built 2026-11-11 (ADR 0035): plan on pull requests, owner-approved and fingerprint-checked apply on dev, verified end to end the same day: PR plan, then an owner-approved apply by the scoped deploy role (ADR 0036, replaces AdministratorAccess). Remaining for Week 10: cost controls and `make destroy-dev`, security checks, demo |

---

## 2. Work packages

### Work package A: AWS foundation and Bronze ingestion (Weeks 1–2)

**A0. Domain and local foundation (Week 1)**
- Write the PostgreSQL operational DDL in `database/postgres/schema/` for `tenants`, `agencies`, `suppliers`, `solicitations`, `awards`, `purchase_orders`, `invoices`, and `contract_documents`, with foreign keys and constraints. Column lists are in `docs/architecture/data_model.md`.
- Add a local PostgreSQL service in `docker/docker-compose.yml`.
- Build a synthetic data generator (`src/procurelake/...` / `jobs/containers/data_generator/`). It must be **seeded and repeatable**, keep relationships valid, and produce at least two tenants (`tenant_alder`, `tenant_birch`) with distinct records.
- Write initial source contracts under `contracts/source/` (awards, suppliers, agencies, invoices).
- Write ADRs for the architectural decisions in `docs/architecture/overview.md` §2.
- Write the SLO definitions (targets in `docs/operations/slos_and_observability.md`).

**A1. Infrastructure (Week 2)**
- Terraform bootstrap (`infrastructure/bootstrap/`) for the remote state backend.
- Reusable modules for networking, storage (S3 lakehouse), KMS, IAM, Glue catalog, Athena workgroups, monitoring, and budgets.
- `infrastructure/live/dev/foundation` root module.
- **AWS Budget and alerts deployed before any persistent infrastructure.**
- Buckets follow the layout in `docs/architecture/overview.md` §4. Bucket names must be globally unique; the names in the plan are illustrative.

**A2. Ingestion framework**
- A Python ingestion framework (`src/procurelake/ingestion/`) with pagination, retries with exponential backoff, checksums, raw-file preservation, ingestion manifests, and incremental collection windows.
- Connectors: USAspending API, Government of Canada procurement CSV, and a generic CSV connector.
- The raw payload and manifest are stored **before** any transformation.
- Public-file ingestion can be reproduced from a pinned download snapshot.

**A3. Completion gate**
- [ ] A fresh Terraform deployment creates the foundation.
- [ ] Representative data is ingested into Bronze.
- [ ] Bronze metadata is registered and queryable in Athena.
- [ ] The successful ingestion run is recorded (manifest/audit).

### Work package B: Silver and Gold lakehouse (Weeks 3–4)

**B1. Silver Iceberg jobs**
- Glue Spark jobs read Bronze, conform schemas, reject malformed records to quarantine with a reason, deduplicate on business keys, and write Iceberg tables registered in the Glue Data Catalog.
- Silver is an explicit, config-driven pipeline framework. Each pipeline's config covers source, table, watermark, validation rules, and target location (`configs/pipelines/`).
- Target tables: `silver_agencies`, `silver_suppliers`, `silver_solicitations`, `silver_awards`, `silver_invoices`, and `silver_contract_intelligence` (the last one is populated in Week 9).
- Business logic goes in `src/procurelake/lakehouse/`. Glue entrypoints in `jobs/glue/` stay thin.

**B2. dbt Gold project**
- `dbt-athena` on Athena engine version 3 with Iceberg table materializations.
- Layers: staging → intermediate → core (dimensions, facts) → marts.
- Models: see `docs/architecture/data_model.md` §4.
- Incremental pattern: `materialized='incremental'`, `table_type='iceberg'`, `incremental_strategy='merge'`, with `unique_key` set. The lookback filter on `source_updated_at` is only a starting point. The real models must handle **deletes and replay explicitly**, and the lookback window must come from a documented late-arrival policy.
- Every model documents its grain, owner, freshness requirement, and tests.

**B3. Completion gate**
- [x] `dbt build` produces the expected Gold models (2026-11-10, ADR 0026).
- [x] Uniqueness, not-null, relationship, financial reconciliation, and business-rule tests pass (49 tests).
- [x] A Gold metric can be traced back to its originating source records (`tests/integration/aws/test_gold_traceability.py`).

### Work package C: CDC and incremental processing (Weeks 5–6)

Flow: PostgreSQL → AWS DMS (full load + CDC) → S3 Bronze CDC (Parquet + source commit timestamps) → Step Functions → Glue CDC apply (validate, order, deduplicate, merge, reconcile) → Silver Iceberg current state (with tombstones and a recovery checkpoint).

**C1. Source configuration**
- Deploy RDS PostgreSQL (`infrastructure/modules/rds_postgresql`) with logical replication enabled (`database/postgres/replication/`).
- Seed a realistic transactional workload with inserts, updates, cancellations, and deletes.

**C2. AWS DMS**
- Full-load-plus-CDC replication to S3 in Parquet format.
- Set the DMS S3 endpoint `TimestampColumnName` setting so source commit timestamps are captured. Confirm the exact setting semantics against current AWS DMS documentation.
- DMS Parquet output is organized by table and **does not preserve full cross-table transaction ordering**. Test the ordering assumptions explicitly. Document when transaction-preserving CSV output or a streaming architecture would be needed.

**C3. Incremental apply engine**
- `src/procurelake/cdc/` covers event parsing, ordering, deduplication, Iceberg merge, checkpoints, reconciliation, and replay. The entrypoint is `jobs/glue/apply_cdc.py`.
- It handles source primary keys, operation types, commit ordering, duplicate input, hard deletes, and reprocessing.
- DynamoDB records completed input manifests and successful checkpoints.
- The checkpoint advances **only after** the Iceberg write and its validation succeed.
- Overlapping writers on the same target table are prevented.

**C4. Failure recovery**
- Simulate a failure after the input is read but before the job completes. On retry, the replay must be safe and must not create duplicate current-state records.

**C5. Completion gate**
- [x] A source change becomes visible in Silver and Gold within the chosen freshness objective. Measured live on 2026-11-10: p95 151 s to Silver and 204 s to Gold, against 15 minutes (ADR 0027). The Glue job and dbt were started by hand, so measure again once Week 6 scheduling is in place. See `docs/evidence/sprint1-week5-cdc-freshness-live.md`.
- [x] Behavior is correct under retries and deletes. Deletes are verified live: 3 deletes are tombstones in Silver and absent from Gold. Retries verified live on 2026-11-10: with the invoices lease held, a scheduled run failed, retried and alerted, and the next run recovered with 0 mismatches (`docs/evidence/sprint1-week6-orchestration-live.md`).

**Orchestration (Week 6):** Step Functions state machines in `orchestration/step_functions/` (batch ingestion, CDC processing, dbt transformation, data quality, document intelligence, tenant exports), scheduled by EventBridge.

### Work package D: Secure multi-tenant data products (Week 7)

**D1. Security design**
- Separate IAM consumer roles for `tenant_alder` and `tenant_birch`.
- Lake Formation row filters and the needed column-level controls.
- Consumer identities are denied direct S3 access to shared lakehouse prefixes.
- Athena query-result locations are controlled (per-tenant Athena workgroups).
- Lake Formation doesn't stop access through other paths to S3, so every path is restricted.

**D2. Delivery surfaces**
- One internal cross-tenant analytics product for authorized administrators.
- Two tenant-scoped data products.
- An Athena query experience plus a scheduled CSV/Parquet export for each tenant (to the products bucket, with explicit IAM and bucket policies).
- A simple dashboard over the governed Gold models (tool not yet chosen; see open questions).

**D3. Completion gate**

Met on 2026-11-10 for the four tenant-facing marts and the daily exports (`make test-aws`, ADR 0030, `docs/evidence/sprint1-week7-tenant-products-live.md`). The dashboard (D2) is deferred by owner decision.

- [x] A query run as Alder returns only Alder records.
- [x] A query run as Birch returns only Birch records.
- [x] Unauthorized table access fails.
- [x] Unauthorized direct S3 access fails.

### Work package E: Reliability, observability, and governance (Week 8; controls introduced from Week 2)

| Component | Implementation |
|---|---|
| Pipeline monitoring | CloudWatch logs and metrics |
| Orchestration | Step Functions execution history |
| Freshness monitoring | Source commit time → Gold availability |
| CDC monitoring | DMS replication lag and task health |
| Data quality | dbt tests and Glue validations |
| Alert delivery | CloudWatch alarms → SNS |
| Failed records | Quarantine prefix with rejection reason |
| Lineage | dbt manifests, source-to-model documentation |
| Catalog governance | Glue metadata, classifications, owners |
| Replay and recovery | Immutable Bronze, checkpoints, targeted backfills |
| Cost tracking | AWS Budgets and Cost Explorer |

SLI targets are in `docs/operations/slos_and_observability.md`. They are **design targets to validate**. Publish the actual measured values.

**Completion gate**
Met on 2026-11-10 with a silent DMS stall drill (ADR 0031, `docs/incidents/2026-11-10-cdc-dms-stall-drill.md`).

- [x] Introduce a controlled CDC failure.
- [x] Monitoring detects it.
- [x] Recover the pipeline.
- [x] Write an incident report with evidence (`docs/runbooks/`, `observability/incident_templates/`).

### Work package F: AI procurement intelligence (Week 9)

Flow: Contract PDF → S3 document store → text extraction (native parser for digital PDFs, Textract for scanned ones) → Amazon Bedrock typed-field extraction with source evidence → Pydantic schema validation, evidence checks, and a human review queue → `silver_contract_intelligence` → `mart_ai_contract_intelligence`.

- Fields to extract, where the source supports them: contract identifier, supplier, issuing agency, contract amount, effective dates, and contract category.
- Prompts live in `configs/ai/prompts/`, model config in `configs/ai/model_config.yaml`, and the extraction schema in `configs/ai/extraction_schema.json`.
- Governance:
  - Every extracted field keeps a source reference.
  - Output is validated against the JSON schema.
  - Prompt and model versions are recorded.
  - Sensitive data is redacted where needed.
  - Uncertain results go to human review.
  - A model confidence score is not treated as proof of accuracy.
  - Missing values are never fabricated.

**Completion gate**
- [ ] A public contract document flows through extraction, validation, and approval into a queryable Gold model.
- [ ] Provenance and quality-evaluation results are recorded.

### Work package G: Optimization, CI/CD, and release (Week 10)

**Performance tests**

| Test | Workload | Measure |
|---|---|---|
| Initial bulk load | 100,000–1,000,000 synthetic records | Records/sec, runtime, cost |
| Incremental CDC | 1,000–10,000 changes | Freshness, correctness, lag |
| Analytical queries | Supplier, agency, and monthly aggregation | Duration, bytes scanned |
| Reprocessing | Replay an already processed batch | Duplicate count, result consistency |
| Concurrent access | Two tenant query sessions | Isolation, query latency |
| AI extraction | 50–100 permitted sample documents | Extraction accuracy, latency, cost |

These sizes are starting workloads for reproducible benchmarking. They make no claim about any real company's production volume.

**Iceberg maintenance:** test partition evolution, data-file sizes, compaction, snapshot history, and query pruning. Athena `OPTIMIZE` rewrites data files, and `VACUUM` expires snapshots and removes orphan files. Choose retention deliberately, because expired snapshots can no longer be used for time travel.

**CI/CD (`.github/workflows/`)**
- PR pipeline: Python format and lint (Ruff), pytest unit tests, `terraform fmt`/`validate`, security scans, `dbt parse`/`compile`, and SQL tests.
- Deploys use GitHub Actions with AWS OIDC. There are no long-lived keys.
- Changes need review, passing automated tests, and a Terraform plan before deployment. Production deploys need manual approval. Only `dev` is fully deployed for now.

**Cost controls**
- The budget and alerts exist before any persistent infrastructure. Alerts are notifications, not caps.
- RDS and DMS run only during CDC development and demos.
- Glue runs on demand, at a sensible job frequency.
- No unnecessary NAT gateway or always-on container infrastructure.
- S3 retention policies are applied while keeping replay and audit data.
- Track Athena bytes scanned, Bedrock requests, Glue compute, and ingestion costs.
- `make destroy-dev` has safeguards and cleans up nonessential resources. It must never delete Terraform state or essential audit artifacts.

**Zero-ETL ADR:** compare DMS-managed CDC with AWS-native Zero-ETL. Support depends on the source engine and version, the destination, and the Region (for example, RDS PostgreSQL Zero-ETL has specific requirements). DMS → S3 → Iceberg stays the primary pipeline. A Zero-ETL proof of concept is an optional extension and must not be presented as equivalent to the custom CDC architecture.

**Security checks, cost review, and recorded demo** (see §4 and §5).

---

## 3. End-to-end acceptance tests

The platform isn't complete until all of these pass, with automated integration tests and a small, reproducible e2e dataset. Unit tests alone are not enough.

| # | Test | Action | Expected result | Test location |
|---|---|---|---|---|
| 1 | Full ingestion | Load a source dataset | Records appear in Bronze and Silver | `tests/e2e/test_full_ingestion.py` |
| 2 | CDC update | Update a PostgreSQL award | The correct latest value appears in Gold | `tests/e2e/test_cdc_to_gold.py` |
| 3 | CDC delete | Delete a source record | The Gold current-state model no longer exposes it | `tests/e2e/test_cdc_to_gold.py` |
| 4 | Replay | Process the same input twice | No duplicate business records | `tests/e2e/` |
| 5 | Late arrivals | Deliver an older event after a newer one | The correct final state is kept | `tests/e2e/` |
| 6 | Schema change | Add an approved nullable field | The pipeline evolves without data loss | `tests/integration/iceberg/` |
| 7 | Data quality | Introduce invalid supplier data | Validation blocks or quarantines it | `tests/integration/` |
| 8 | Tenant isolation | Query as Tenant Alder | No Birch data is visible | `tests/security/test_tenant_isolation.py` |
| 9 | Unauthorized access | Try direct access to restricted S3 data | Access denied | `tests/security/test_unauthorized_access.py` |
| 10 | Pipeline failure | Interrupt a CDC apply job | Recovers from a known checkpoint | `tests/e2e/test_pipeline_recovery.py` |
| 11 | AI evaluation | Process a document with missing fields | Unsupported values are not fabricated | `tests/e2e/test_ai_document_to_gold.py` |
| 12 | Performance | Compare initial and optimized queries | Actual measurements are reported | `tests/performance/`, `docs/benchmarks/` |

---

## 4. Portfolio deliverables (release checklist)

- [ ] Architecture diagram and design decisions
- [ ] Terraform modules and deployment guide
- [ ] PostgreSQL schema and synthetic data generator
- [ ] Ingestion and CDC pipelines
- [ ] Silver Iceberg and Gold dbt models
- [ ] Data-quality and reconciliation reports
- [ ] Lake Formation tenant-isolation tests
- [ ] CloudWatch operational dashboard
- [ ] Bedrock document-processing demonstration
- [ ] Performance and cost benchmark report
- [ ] Incident recovery runbook
- [ ] Recorded 5–10 minute end-to-end demo

### End-to-end demo script (one coherent recording)
1. Show a procurement award in PostgreSQL.
2. Update the award amount and show AWS DMS capturing the event.
3. Show the change arriving in Bronze S3.
4. Run the incremental Glue job and inspect the Silver Iceberg state.
5. Run dbt and show the changed Gold KPI.
6. Show that a different tenant cannot see the record.
7. Show the pipeline metrics and passing quality tests.
8. Process a related public contract document through Bedrock and show the approved AI-derived attributes.

---

## 5. Sprint 1: first five working days

| Day | Tasks | Definition of done |
|---|---|---|
| 1 ✅ | Initialize the repository and ADRs; local Docker PostgreSQL; database schema | All source tables exist with constraints. **Done 2026-11-10:** 8 tables in schema `procurement`, verified by 39 passing tests (ADRs 0001–0012, 0015–0017) |
| 2 ✅ | Synthetic procurement generator with two tenants | Repeatable seeded dataset with valid relationships. **Done 2026-11-10:** seed 42 gives fingerprint `350d064e…` with 5,702 rows across both tenants. Loads under every DB constraint and round-trips exactly. 75 tests (ADR 0018) |
| 3 ✅ | Terraform backend, AWS authentication, KMS, buckets, IAM, budget alerts | `terraform plan` succeeds; foundation deploys. **Done 2026-11-10** (ADR 0019): bootstrap (16) and dev/foundation (34) applied in `ca-central-1`. State is in S3, both stacks plan "No changes", every resource was checked through the AWS CLI, and 21 offline Terraform tests pass |
| 4 ✅ | First public API/CSV ingestion with a manifest | Raw source data and metadata land in S3. **Done 2026-11-10** (ADR 0020): live USAspending window 2026-10-23 landed in dev Bronze (56 pages, 5,542 records, reconciled with the source count). Manifest is in artifacts; checksums, encryption and immutability were verified in S3. 107 tests. The Canada CSV connector is still to do |
| 5 ✅ | Glue Catalog and Athena; first integration tests | Bronze data is queryable; test evidence recorded. **Done 2026-11-10** (ADR 0021): Bronze Glue table plus the engineering workgroup were applied (5 resources; no drift). 6/6 live integration tests passed. Evidence: `docs/evidence/sprint1-day5-bronze-queryable.md` |

The sprint should end with a reproducible commit and a working foundation, not just a diagram.

### Repository foundation checklist (first commit)
- [x] Create the GitHub repository and branch protection (public repo; `dev` default; `main` protected)
- [x] Initialize the Python package, `pyproject.toml`, and `uv.lock`
- [x] Configure pytest, Ruff, pre-commit, and CI (`.github/workflows/ci-python.yml`, `ci-terraform.yml`; `.pre-commit-config.yaml`)
- [x] Set up Terraform bootstrap and the dev environment
- [x] Create the local PostgreSQL Docker Compose setup and schema
- [x] Create initial source contracts and configuration files (`contracts/source/` checked against the live schema; `configs/tenants/`)
- [x] Implement the synthetic procurement data generator
- [x] Add architecture ADRs and the README

Don't create every directory up front. Scaffold the core and grow each domain as it is built.

**Next engineering artifact after this plan:** the Sprint 1 technical design and Terraform foundation, covering the PostgreSQL DDL, synthetic data generator, AWS resource inventory, security model, and deployment sequence.

---

## 6. Success criteria

| Dimension | Must be explainable |
|---|---|
| Architecture | Why Iceberg, Glue, Athena, and dbt have distinct responsibilities |
| Scalability | How ingestion and incremental processing behave as volume grows |
| Correctness | How ordering, retries, duplicates, deletes, and reconciliation are handled |
| Governance | How tenant isolation is enforced across both query and storage paths |
| Reliability | How an incident is detected, recovered from, and prevented from recurring |
| Cost | What drives AWS cost and which optimizations were measured |
| AI | How model output is validated before it becomes a trusted data product |
| Leadership | Which tradeoffs, risks, decisions, and standards were set |
