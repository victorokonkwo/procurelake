# ADR 0047: Data and platform policy decisions (currency, suppliers, public data, Object Lock, Glue version)

- **Status:** Accepted (owner decisions 2026-11-14; resolves open questions 10, 11, 12, 28, 31
  and 35). Policy only: no code or infrastructure changes.
- **Date:** 2026-11-14

## Context
Several open questions recorded choices that the code already follows by default, without a
decision behind them. On 2026-11-14 the owner decided them. Each decision below states what is
now policy and what would need a new ADR to change.

## Decision
1. **No currency conversion (open question 10).** Every monetary value keeps its source currency
   (AGENTS.md §2.4). Reports are per currency. Nothing sums or compares amounts across
   currencies. A cross-currency report needs a new ADR that names an authoritative rate source,
   the rate date used (transaction, award or reporting date) and how the converted value is
   labelled. Until then no exchange-rate data is ingested.
2. **No supplier matching across tenants or names (open questions 11 and 35, merged).**
   `dim_supplier` stays one row per supplier registration within a tenant (ADR 0026). There is
   no canonical supplier across tenants, no name-based entity resolution, and no cross-tenant
   supplier reporting. Public reference suppliers (USAspending recipients, Canada vendors) are
   not matched to tenant suppliers. Changing this needs a new ADR with an entity-resolution
   policy: the identifiers allowed, how a wrong match is found and reversed, and who may see the
   result.
3. **Public reference data is separate and admin-only (open question 12).** USAspending and
   Government of Canada data stay in their own reference Silver tables and any reference Gold
   models, with their source provenance (AGENTS.md §2.3). They are never joined into tenant marts.
   Tenant roles are not granted them, so the tenant row filters and isolation tests (ADR 0030) are
   unchanged. Only the admin product may read them.
4. **Object Lock: not in dev, required for production Bronze (open question 28).** Dev Bronze
   keeps versioning plus the IAM delete denial (ADR 0019, ADR 0020). Object Lock can only be
   turned on when a bucket is created, so a staging or production Bronze bucket must be created
   with it, in governance mode, with a retention period decided at that time. Dev's buckets are
   not migrated.
5. **Glue stays on 5.1 (open question 31).** Glue 6.0+ is listed at $0.308 per DPU-hour against
   $0.44 for 5.1 in ca-central-1 (AWS Pricing API, checked 2026-11-10). Moving changes the
   Spark, Python and Iceberg versions fixed in ADR 0015, every Glue job and the local Spark test
   stack. At dev volumes the saving is small. Revisit before scaling the Silver jobs, or when
   AWS announces end of support for Glue 5.1.

## Alternatives considered
- **Exchange rates:** Bank of Canada daily rates as a reference source with a converted CAD
  column, or a seeded rate table for demos. Not chosen: no report needs conversion yet, and a
  seeded table would not be authoritative.
- **Suppliers:** exact matching on registered identifiers (US UEI, Canadian business number) in
  an admin-only mapping, or fuzzy name matching with review. Not chosen: no report needs it, and
  a wrong merge across tenants is a data-isolation risk.
- **Public data:** a shared read-only mart for tenants, or joining it into tenant marts. Not
  chosen: it adds grants and isolation tests, and mixes provenance.
- **Object Lock in dev now:** needs new buckets and a Bronze migration, and locked objects
  can't be cleaned up during retention.
- **Glue 6.0 now:** a version change across every job for a small saving at dev volumes.

## Consequences
- These are constraints on future work: a currency conversion, supplier matching, public data
  in tenant products, or a production Bronze bucket without Object Lock each needs a new ADR.
- Open question 29 (human AWS access) stays open by owner decision.
