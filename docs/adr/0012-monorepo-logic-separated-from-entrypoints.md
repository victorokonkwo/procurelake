# ADR 0012: Domain-oriented monorepo; business logic separated from execution entrypoints

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Five capabilities share one data model and set of standards. Logic must be testable without AWS runtimes.

## Decision
One monorepo. Reusable logic lives in `src/procurelake/` (ingestion, cdc, lakehouse, ai, data_products, common). Files in `jobs/` (Glue, Lambda, containers) are thin entrypoints that call into it. For example, `jobs/glue/apply_cdc.py` invokes `procurelake.cdc.iceberg_merge`. The library ships as a versioned wheel.

## Consequences
- Unit tests cover the logic without Glue.
- The repository doesn't need reorganising as modules are added.
- Directories are created only when real content lands in them.

## Alternatives considered
Separate repos per module were rejected because definitions would drift between them.
