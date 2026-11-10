# ADR 0010: CI/CD with GitHub Actions (OIDC) and Terraform

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Deployments must be repeatable and auditable, without long-lived credentials.

## Decision
- All infrastructure is defined in Terraform. Reusable modules live in `infrastructure/modules/`. Each `infrastructure/live/<env>/<area>/` is an independent root module (`foundation`, `data_platform`, `security`, `observability`) with its own remote state.
- GitHub Actions authenticates to AWS through OIDC, with no stored access keys.
- PRs run lint, unit tests, `terraform fmt`/`validate`, security scans, and `dbt parse`/`compile`.
- Deploys require review, passing tests, and a Terraform plan. Production deploys require manual approval.

## Consequences
- Terraform never contains Python transformations or business SQL.
- Provider versions must be pinned.

## Alternatives considered
None recorded in the plan.
