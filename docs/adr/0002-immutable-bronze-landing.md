# ADR 0002: Immutable, versioned S3 raw landing (Bronze)

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
The platform must support replaying data and auditing what was received.

## Decision
Bronze is a set of S3 objects that are never destructively modified, in a bucket with versioning enabled. The raw payload and its manifest (`source_system`, `ingestion_timestamp`, `source_file`, `batch_id`, `schema_version`, checksums) are written **before** any transformation. Invalid data goes to `quarantine/` with a rejection reason.

## Consequences
- Silver and Gold can always be rebuilt from Bronze.
- Storage grows with each delivery. S3 retention policies must keep the data needed for replay and audit (see cost controls).

## Alternatives considered
Transforming data while landing it was rejected because replay would then be impossible.
