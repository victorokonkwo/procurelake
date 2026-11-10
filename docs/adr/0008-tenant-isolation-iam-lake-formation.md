# ADR 0008: Tenant isolation with IAM + Lake Formation + S3 path denial

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Two synthetic tenants (`tenant_alder`, `tenant_birch`) share one platform but must get isolated results. Lake Formation fine-grained access applies to queries through integrated engines such as Athena. It is not a substitute for restricting every other path to S3.

## Decision
- Each tenant has its own IAM consumer role and Athena workgroup, with controlled query-result locations.
- Lake Formation row filters and column-level controls apply to the governed tables.
- Consumer roles are explicitly **denied** direct S3 access to the shared lakehouse prefixes.
- Tenant exports go to a separate products bucket with explicit IAM and bucket policies.
- Every tenant-owned source row carries a `tenant_id` that the database validates (ADR 0016).

## Consequences
- Isolation must be proven by negative tests: `tests/security/test_tenant_isolation.py` and `test_unauthorized_access.py`.

## Alternatives considered
Relying on Lake Formation alone was rejected because direct S3 paths would remain open.
