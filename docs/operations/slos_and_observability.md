# SLOs, observability, and cost controls

## 1. Service level indicators and initial targets

These are **design targets to validate, not measured results**. Collect actual values from tests and publish them in `docs/benchmarks/` and `observability/slo/`. Never report a target as if it had been achieved.

| SLI | Initial target |
|---|---|
| CDC freshness | 95% of source changes reach Gold within 15 minutes |
| Batch freshness | New public-data batches processed within 24 hours |
| CDC correctness | No duplicate active business keys after replay |
| Tenant isolation | Zero cross-tenant records returned in negative tests |
| Critical business quality | 100% of critical dbt checks pass before publishing |
| Recovery | Failed batches can be replayed without manual data repair |

Freshness is measured from the **source commit time** (captured by the DMS `TimestampColumnName` setting) to **Gold availability**.

### Measured so far (dev, synthetic data)

| SLI | Measured | Where |
|---|---|---|
| CDC freshness | p95 190–204 s to Gold in two normal runs (42 and 47 changes). In the stall drill, 18.5 min for the 25 stalled changes (target missed, alarm fired) | `docs/evidence/sprint1-week5-cdc-freshness-live.md`, `docs/incidents/2026-11-10-cdc-dms-stall-drill.md` |
| CDC correctness | 0 duplicate keys in Silver after replay and after the stall recovery; 0 lost changes | Week 5 evidence, the incident report |
| Tenant isolation | 0 cross-tenant rows in queries, exports and the access check; the drilled foreign-row case was caught | `docs/evidence/sprint1-week7-tenant-products-live.md` |
| Recovery | Stall: resume from the DMS checkpoint, all changes in Gold 5.8 min later, no manual data repair | incident report |

Each is one measurement in dev, not a sustained SLO.

## 2. Observability components

| Component | Implementation |
|---|---|
| Pipeline monitoring | CloudWatch logs and metrics |
| Orchestration | Step Functions execution history |
| Freshness monitoring | Source commit time → Gold availability |
| CDC monitoring | DMS replication lag and task health |
| Data quality | dbt tests and Glue validations |
| Alert delivery | CloudWatch alarms and EventBridge → SNS (KMS-encrypted topic; `dev/observability`, `dev/cdc`; ADR 0031) |
| Failed-record management | Quarantine prefix with rejection reason |
| Lineage | dbt manifests; `make lineage`: PostgreSQL table → Bronze prefix → Silver → Gold, with owners and classifications (ADR 0042) |
| Catalog governance | Owner, classification, grain and description as Glue table metadata: Silver from the source contracts (daily maintenance job), Gold from dbt `persist_docs`; `make catalog-check` reports drift (ADR 0042) |
| Replay and recovery | Immutable Bronze, checkpoints, targeted backfills |
| Cost tracking | AWS Budgets and Cost Explorer |

Controls start in Week 2 and are completed in Week 8.

## 3. Incident drill (Week 8 completion gate)

1. Introduce a controlled CDC failure.
2. Detect it through monitoring and alarms.
3. Recover the pipeline from a known checkpoint.
4. Write an incident report with evidence, using a template from `observability/incident_templates/`, and add the runbook to `docs/runbooks/`.

## 4. Cost controls

- Create an AWS Budget and alerts **before** deploying any persistent infrastructure. Budget alerts are notifications, not hard caps.
- Run RDS and DMS only during CDC development and demos.
- Run Glue on demand and keep job frequency modest.
- Don't add a NAT gateway or always-on container infrastructure unless it's needed.
- Apply S3 retention policies, but keep the data needed for replay and audit.
- Track Athena bytes scanned, Bedrock requests, Glue compute, and ingestion costs.
- Provide `make destroy-dev` with safeguards. It must never delete Terraform state or essential audit artifacts.
