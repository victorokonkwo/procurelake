# ADR 0011: Single-account `dev` first; multi-account is the evolution path

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
The project is a portfolio-grade reference implementation and should avoid paying for unnecessary infrastructure.

## Decision
Deploy only `dev`, in one AWS account. Ingestion, engineering, and consumer identities are separated with IAM roles. The staging and prod Terraform roots keep the target structure (separate accounts per environment) but aren't deployed.

## Consequences
- Lower cost and simpler operations.
- Account-level blast-radius isolation is documented, not demonstrated.

## Alternatives considered
Separate accounts from day one was deferred because of cost and setup overhead.
