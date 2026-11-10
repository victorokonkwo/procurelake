# ADR 0016: Operational (OLTP) schema design

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `database/postgres/migrations/V0001__operational_schema.sql`

## Context
The plan lists the eight source tables and their key columns. Planned downstream models need columns the plan doesn't list: `mart_procurement_cycle_time` needs an award timestamp, incremental models and CDC need `source_updated_at`, and `mart_actual_spend` reports actual spend by agency. The plan also requires that every tenant-owned record have a trustworthy `tenant_id`, validated at the transactional layer.

## Decision
1. **Schema `procurement`.** Source tables live in a dedicated schema, so DMS table mappings can include just that schema. Migration bookkeeping (`public.schema_migrations`) stays outside it.
2. **Keys.** `tenant_id` is a text slug matching `^tenant_[a-z][a-z0-9_]*$` (for example `tenant_alder`). All other primary keys are `uuid`, defaulting to `gen_random_uuid()`.
3. **The database enforces tenant integrity.** Every tenant-owned table has `UNIQUE (tenant_id, <pk>)`, and every child table references its parent through a composite `(tenant_id, <fk>)` foreign key. A row can't reference a parent belonging to another tenant.
4. **Columns added beyond the plan:**
   - `created_at` / `updated_at` (`timestamptz`) on every table. A `BEFORE UPDATE` trigger keeps `updated_at` current, and it becomes `source_updated_at` downstream.
   - `tenants.is_synthetic` (NOT NULL), so synthetic tenants are explicitly labelled.
   - `solicitations.title` and `closing_at`; `awards.awarded_at` and `status` (`active` / `cancelled`). The plan's workload includes cancellations.
   - `purchase_orders.agency_id` (required), `award_id` (optional), `currency_code`, and `status`.
   - `invoices.invoice_date`, `invoice_amount`, `currency_code`, and `paid_at`.
5. **Currency.** The plan's `currency` column is named `currency_code`: `char(3)` holding an uppercase ISO-4217-format code. Every monetary column carries a currency. A composite foreign key forces each invoice into its purchase order's currency.
6. **Money** is `numeric(18,2)`. Amounts must be positive, and `paid_amount` must be between 0 and `invoice_amount`.
7. **Lifecycle rules are CHECK constraints.** Allowed status values are listed. Any solicitation past `draft` (other than `cancelled`) must have `published_at`. `closing_at` must be after `published_at`. `payment_status` must agree with `paid_amount` and `paid_at`.
8. **The issuing agency of an award** comes from `solicitation_id` and is not stored twice on `awards`.
9. **Supplier names are not unique.** Canonicalising suppliers is a Silver/Gold job (`dim_supplier`), and realistic OLTP data contains near-duplicates.
10. **Hard deletes are allowed. Foreign keys use the default `NO ACTION`,** so a parent with children can't be deleted. The CDC workload deletes children before their parents.
11. **Logical replication is enabled locally** (`wal_level=logical`) so CDC behaviour can be tested before RDS and DMS are set up.

## Consequences
- The plan's "tenant isolation" property holds at the source, not only in the lake.
- The currency format check doesn't validate codes against the ISO-4217 list. A real code list (a reference table or seed) can be added later.
- `numeric(18,2)` can't represent currencies with 3 minor-unit digits. That's acceptable for the synthetic tenants' currencies; revisit it if one is added that needs more.
- Some rules can't be expressed as CHECK constraints, such as `awarded_at >= solicitation.published_at`. They are left to Silver validation rather than enforced with triggers.

## Alternatives considered
- **Use only the plan's columns.** Rejected: cycle time and incremental CDC would be blocked.
- **Bigint identity keys.** Rejected: UUIDs let the generator and multiple sources create keys without coordinating, and they avoid leaking row counts.
