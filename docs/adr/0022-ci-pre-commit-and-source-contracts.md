# ADR 0022: CI, pre-commit hooks, and source contracts

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
The Sprint 1 foundation checklist still had three open items: CI, pre-commit hooks, and initial source contracts. The plan requires PR checks covering formatting, linting, unit tests, Terraform validation, and SQL/dbt checks. It also requires data contracts that are versioned and actually enforced.

## Decision
1. **GitHub Actions**, on every pull request and on pushes to `dev`/`main`, with `permissions: contents: read` and no AWS access:
   - `ci-python`: Ruff lint and format check, plus pytest against a PostgreSQL 17.11 service container. This covers unit tests, the schema and migration tests, generator load tests, and the contract-versus-schema tests. The AWS integration tests skip, because they are opt-in.
   - `ci-terraform`: `terraform fmt -check` plus `validate` and `terraform test` with the mock provider, for every module and root.
   - Actions are **pinned to full commit SHAs**: `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.3, `hashicorp/setup-terraform` v4.0.1. Their inputs were checked against each action's `action.yml` at that SHA. Tool versions are pinned to match local: uv 0.13.7, Python 3.11, Terraform 1.17.1.
2. **pre-commit** (`.pre-commit-config.yaml`): whitespace, end-of-file and line-ending fixes; YAML/TOML/JSON syntax; merge conflicts; files over 1 MB; private keys; AWS credentials; Ruff check and format (pinned `v0.17.4`, the same version as the dev dependency); and `terraform fmt`. All hooks pass on the whole repository.
3. **Source contracts** (`contracts/source/*.yaml`, validated by `procurelake.contracts.source`) cover agencies, suppliers, awards and invoices. Each declares grain, owner, classification, tenant scoping, primary and foreign keys, the change-capture column and delete semantics, and every column's type, nullability and description. Allowed values are declared where a CHECK constraint lists them. **A PostgreSQL test compares each contract with the migrated catalog.** Breaking a contract on purpose (nullability, type, an extra allowed value) fails that test.
4. **Compatibility rule:** within a major `contract_version`, the only change allowed is adding a nullable column.

## Consequences
- Terraform plans with OIDC in CI are not set up yet. They need the backend configuration (the account-specific bucket) as GitHub variables, and the plan role from ADR 0019.
- No dependency, IaC or full-history secret scanner yet (open question 5).
- Contracts for tenants, solicitations, purchase_orders, contract_documents and the USAspending source are still to be written. The validator and test harness already accept them.
