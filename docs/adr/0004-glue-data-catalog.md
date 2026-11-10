# ADR 0004: AWS Glue Data Catalog as the shared catalog

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Glue (Silver) and Athena/dbt (Gold) need to see the same table metadata, and Lake Formation needs a catalog to attach permissions to.

## Decision
The AWS Glue Data Catalog is the single catalog for Bronze, Silver, and Gold tables. Catalog governance (classifications, owners) lives in Glue metadata.

## Consequences
- One metadata source for every engine.
- Lake Formation permissions are granted on catalog databases and tables.

## Alternatives considered
None recorded in the plan.
