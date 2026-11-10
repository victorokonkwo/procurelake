# ADR 0001: Primary AWS region `ca-central-1`

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
The platform simulates a procurement SaaS serving government agencies, and the plan sets a Canadian-region deployment as the default.

## Decision
Deploy all regional resources to `ca-central-1` (Canada Central).

## Consequences
- Each service and feature the plan relies on must be confirmed available in `ca-central-1` before its week starts. This is especially true for Bedrock models, Textract features, Lake Formation on Iceberg, and Zero-ETL integrations (open question 18). Anything unavailable gets its own ADR. Don't silently switch regions.
- Global resources (IAM, Budgets) aren't affected.

## Alternatives considered
No alternative region was evaluated in the plan.
