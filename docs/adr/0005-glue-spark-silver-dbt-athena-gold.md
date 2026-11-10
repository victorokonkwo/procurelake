# ADR 0005: Glue Spark for Silver; dbt on Athena for Gold

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Silver logic (CDC apply, complex cleansing, deduplication) is procedural and stateful. Gold logic (dimensions, facts, marts) is relational and benefits from modular, testable SQL.

## Decision
- **Silver:** AWS Glue Spark jobs (Glue 5.1, see ADR 0015) driven by an explicit, config-driven pipeline framework.
- **Gold:** dbt with `dbt-athena` on Athena engine version 3, using Iceberg materializations and `incremental_strategy='merge'`.

## Consequences
- Each engine has its own responsibility, and the boundary is clear.
- dbt incremental models must handle deletes and replay explicitly, and the lookback window must come from a documented late-arrival policy (open question 8).
- Versions of dbt-core and dbt-athena must be pinned (open question 2).

## Alternatives considered
Doing all transformation in Spark was rejected because SQL models are easier to test and document. Doing all transformation in dbt was rejected because CDC apply and cleansing need procedural control.
