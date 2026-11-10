# ADR 0003: Apache Iceberg on S3 for Silver and Gold

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Silver needs CDC merges, deletes, and schema evolution. Gold needs incremental merges. All of it must stay queryable from Athena.

## Decision
Silver and Gold tables are Apache Iceberg tables on S3, registered in the Glue Data Catalog.

## Consequences
- We get ACID transactions, schema evolution, and time travel.
- Tables need maintenance: Athena `OPTIMIZE` for compaction and `VACUUM` for snapshot expiry and orphan-file cleanup. Expired snapshots are lost for time travel, so retention needs an explicit decision (open question 21).
- Never create folders by hand under Iceberg table locations.

## Alternatives considered
The plan does not record a comparison with other table formats.
