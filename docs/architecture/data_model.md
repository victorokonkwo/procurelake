# Data model and engineering contracts

Define these before you write pipelines, so the five modules don't drift into inconsistent definitions. Machine-readable contracts go in `contracts/` (`source/`, `products/`, `schemas/json`, `schemas/avro`).

## 1. Source datasets

| Source | Dataset | Ingestion pattern |
|---|---|---|
| Simulated PostgreSQL OLTP | Agencies, suppliers, solicitations, awards, purchase orders, invoices | AWS DMS full load + CDC |
| USAspending API (`api.usaspending.gov`) | Public US federal awards, agencies, recipients | Python API ingestion |
| Government of Canada Open Data | Published procurement contracts | Scheduled CSV ingestion |
| Public contract PDFs | Contract text and metadata | S3 + Textract + Bedrock |
| Synthetic application events | Tenant and procurement activity | Python events (streaming is an optional extension) |

The transactional database is the main CDC source. Public records are kept in a **separate reference dataset** with their original provenance preserved.

## 2. PostgreSQL operational schema

**Implemented** in `database/postgres/migrations/V0001__operational_schema.sql`, schema `procurement`. The design rationale is in ADR 0016. The table below lists the columns the plan specified, plus the columns ADR 0016 added (in *italics*). Every table also has `created_at` and `updated_at`, and `updated_at` is maintained by a trigger and used as `source_updated_at` downstream.

| Table | Primary key | Columns |
|---|---|---|
| `tenants` | `tenant_id` (text, `^tenant_[a-z][a-z0-9_]*$`) | tenant_name, status (`active`/`suspended`/`offboarded`), *is_synthetic* |
| `agencies` | `agency_id` (uuid) | tenant_id, agency_name (unique per tenant) |
| `suppliers` | `supplier_id` (uuid) | tenant_id, supplier_name, category |
| `solicitations` | `solicitation_id` (uuid) | tenant_id, agency_id, *title*, published_at, *closing_at*, status (`draft`/`published`/`closed`/`awarded`/`cancelled`) |
| `awards` | `award_id` (uuid) | tenant_id, solicitation_id, supplier_id, award_amount, *currency_code* (plan: `currency`), *awarded_at*, *status* (`active`/`cancelled`) |
| `purchase_orders` | `po_id` (uuid) | tenant_id, *agency_id*, supplier_id, *award_id* (nullable), order_date, amount, *currency_code*, *status* (`open`/`closed`/`cancelled`) |
| `invoices` | `invoice_id` (uuid) | tenant_id, po_id, *invoice_date*, *invoice_amount*, paid_amount, *currency_code*, payment_status (`pending`/`partially_paid`/`paid`/`cancelled`), *paid_at* |
| `contract_documents` | `document_id` (uuid) | tenant_id, award_id, document_s3_key (object key, no `s3://`) |

Constraints the database enforces:
- **Tenant integrity:** composite `(tenant_id, fk)` foreign keys make cross-tenant relationships impossible.
- **Money:** amounts are `numeric(18,2)` and must be positive. `currency_code` must match `^[A-Z]{3}$`. Each invoice is in its PO's currency (composite FK). Invoices can't be overpaid.
- **Lifecycle:** status enums. A solicitation that has been published needs `published_at`. `closing_at` must be after `published_at`. `payment_status` must agree with `paid_amount` and `paid_at`.
- Hard deletes of a parent that still has children are rejected.

Tests: `database/postgres/tests/test_operational_schema.py`.

Rules:
- Every tenant-owned record has a trustworthy `tenant_id`, validated at the transactional layer.
- Generate at least two synthetic tenants, `tenant_alder` and `tenant_birch`, with distinct records so isolation can be proven (Day 2).
- An award's issuing agency is derived through `solicitation_id`.

## 3. Layer contracts

### Bronze: source fidelity and replay
- Keep incoming source files and CDC events with **no destructive transformations**.
- Ingestion metadata goes in manifests or envelopes: `source_system`, `ingestion_timestamp`, `source_file`, `batch_id`, `schema_version`, and checksums.
- Invalid records go to `quarantine/` with a rejection reason.

### Silver: correct, standardized business entities
- Tables: `silver_agencies`, `silver_suppliers`, `silver_solicitations`, `silver_awards`, `silver_invoices`, and `silver_contract_intelligence`.
- Silver handles deduplication, type validation, current-state CDC application, deletes (tombstones), and source provenance.
- Format: Apache Iceberg, registered in the Glue Data Catalog.

### Gold: consumption-ready business products
- Dimensions, facts, KPI marts, tenant-scoped products, and AI consumption datasets, built with dbt on Athena.
- Every model documents its **grain, ownership, freshness requirement, and quality tests**.

## 4. Gold models

| dbt model | Grain / purpose |
|---|---|
| `dim_supplier` | One canonical supplier |
| `dim_agency` | One government agency |
| `dim_date` | One calendar day |
| `fct_contract_award` | One procurement award |
| `fct_purchase_order` | One purchase order |
| `fct_invoice` | One invoice |
| `mart_awarded_value` | Award values by agency, supplier, and month |
| `mart_supplier_concentration` | Supplier share of awarded value |
| `mart_procurement_cycle_time` | Time from solicitation publication to award |
| `mart_actual_spend` | Paid invoice amounts by supplier and agency |
| `mart_ai_contract_intelligence` | Approved extracted contract attributes |

dbt folder placement (from the repository structure): `models/core/dimensions/`, `models/core/facts/`, `models/marts/procurement/` (awarded value, supplier concentration, cycle time), `models/marts/finance/` (actual spend), `models/marts/tenant_analytics/`, and `models/marts/ai_intelligence/`. Staging is split by source: `staging/postgres/`, `staging/usaspending/`, `staging/canada_contracts/`. Intermediate is split by domain: `intermediate/procurement/`, `intermediate/finance/`.

## 5. Metric semantics

- **Awarded value** (from awards) and **actual spend** (from paid invoices) are different metrics. Never call award totals "spend."
- **Currency:** every monetary value keeps its source currency. Cross-currency reporting needs a documented exchange-rate source and conversion policy. Until that exists, never aggregate across currencies.
- **Procurement cycle time:** measured from `solicitations.published_at` to `awards.awarded_at`.

## 6. Data product contracts

The first planned product contracts in `contracts/products/` are:
- `supplier_spend.yaml`
- `procurement_performance.yaml`
- `contract_intelligence.yaml`

Each one specifies grain, owner, freshness, schema, and compatibility rules, and is versioned.

## 7. AI extraction fields

Extract these fields from contract documents when the source supports them: contract identifier, supplier, issuing agency, contract amount, effective dates, and contract category. Each field carries a source-evidence reference, plus the prompt version and model version used. The schema lives in `configs/ai/extraction_schema.json`.
