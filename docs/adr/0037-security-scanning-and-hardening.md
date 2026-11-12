# ADR 0037: Security scanners in CI, and the hardening they prompted

- **Status:** Accepted
- **Date:** 2026-11-11
- **Implementation:**
  - Workflow and config: `.github/workflows/ci-security.yml`, `.checkov.yaml`,
    `.gitleaksignore`
  - Inline `checkov:skip` waivers on the resources they cover
  - Hardening:
    - role trusts: `live/dev/security` (tenant and admin roles) and `live/dev/data_platform`
      (AI extraction role);
    - the dbt task-tracking policy (`live/dev/data_platform`);
    - default security groups (`modules/public_network`, `modules/cdc_network`)
  - Tests: `tenant_and_admin_roles_trust_only_named_principals` (security)
  - Local run: `make security-scan`
- **Resolves:** open question 5.

## Context
Work package G's security checks. The pre-commit hooks caught only private keys and AWS keys
in staged files, and there was no IaC or dependency scanning (open question 5).

The owner chose on 2026-11-11 Checkov for Terraform, pip-audit for Python dependencies and
gitleaks for secrets across the full history. Each finding is fixed, or waived with a written
reason.

## Decision
1. **Pinned tools**, each at least two weeks old:
   - Checkov 3.3.30 (PyPI, 2026-10-20), run with `uvx`;
   - pip-audit 2.10.1 (PyPI), run with `uvx`;
   - gitleaks v8.30.1, the release binary, checked against the SHA-256 in its release checksums
     file.
2. **Workflow `ci-security`:** runs on pull requests, on pushes to `dev`/`main`, every Monday
   (new advisories appear without code changes) and on demand. Any new finding fails it.
   - **Checkov** scans all Terraform under `infrastructure/`.
   - **pip-audit** checks every dependency group in `uv.lock`, hash-checked, without
     installing anything.
   - **gitleaks** scans every commit, fully fetched, and redacts what it prints.
3. **Fixes made:**
   - **Role trusts:** the tenant, admin and AI-extraction roles trusted the whole account
     (`CKV_AWS_61`). An `aws:PrincipalArn` condition now limits them to the operator, plus,
     for the tenant roles, the exports and access-check state machine roles that act as a
     tenant. The operator list is a new input, `operator_arns`, which CI passes.
   - **Default security groups:** each VPC's default group is now managed with no rules, so it
     denies everything (`CKV2_AWS_12`).
   - **dbt orchestration policy:** `ecs:StopTask` and `ecs:DescribeTasks` were allowed on `*`.
     They are now limited to the dbt cluster's tasks (`CKV_AWS_290`, `CKV_AWS_355`).
4. **Waived per resource** (`checkov:skip` on the resource, with its reason):
   - **False positives:** the lakehouse buckets' public access block, KMS encryption and
     versioning (separate resources, verified live, ADR 0019); two security groups attached
     elsewhere; the DMS S3 target, which does use SSE-KMS with the data key.
   - **Trusts:** the four role trusts above (Checkov ignores conditions).
   - **Disposable CDC stack:** RDS multi-AZ, deletion protection, IAM auth, monitoring and
     public access, the replication secret's rotation, the DMS minor upgrades and source
     endpoint key. That stack holds synthetic data, is reachable only from operator CIDRs with
     TLS forced, and is destroyed after every session (ADR 0027).
   - **Availability-zone pinning:** a changed zone list would show as a planned subnet
     replacement, never silently.
5. **Waived repository-wide** (`.checkov.yaml`; dev scope, revisit before production):

   | Check | Why it is not applied in dev |
   |---|---|
   | `CKV_AWS_144` S3 cross-region replication | One region by design (ADR 0001, 0011); Bronze is versioned and never expires |
   | `CKV_AWS_18` S3 access logging | CloudTrail covers management events; object access logs are a production control |
   | `CKV2_AWS_62` S3 event notifications | No consumer; pipelines are scheduled |
   | `CKV_AWS_338` log retention of 1 year | 30 days for cost; lasting evidence is written to S3 |
   | `CKV_AWS_158` log group KMS | Default encryption at rest; a customer-managed key needs a key-policy grant (follow-up) |
   | `CKV_AWS_195` Glue security configuration | Output is SSE-KMS by bucket default; bookmarks off; logs as above |
   | `CKV_AWS_284`, `CKV_AWS_285` Step Functions tracing and logging | 90-day execution history, and failure alarms on every state machine |
   | `CKV_AWS_297` Scheduler customer-managed key | Payloads are only job or state machine names |
   | `CKV2_AWS_11` VPC flow logs | No ingress; the CDC VPC is disposable |
   | `CKV_AWS_65` ECS Container Insights | One short task per run, with task logs and alarms |

6. **gitleaks waiver:** one fingerprint. ADR 0032 quotes an AWS access-denied message
   containing a Bedrock model id, which the `generic-api-key` rule reads as a key.

## Consequences
- **First results (2026-11-11, local, same versions):**
  - **Checkov:** 73 failed of 541 checks. After the fixes and waivers: 469 passed, 0 failed,
    32 waived inline, plus the repository-wide skips.
  - **pip-audit:** no known vulnerabilities in any locked group.
  - **gitleaks:** 63 commits scanned. The one finding was the false positive above; with it
    waived, nothing was found.
- **The working-tree scan finds 45 matches, all in git-ignored local files:** `.env` (the
  operator's own AWS key) and `build/` evidence (UUIDs). CI scans history only, which never
  contained them.
- **Applied by CI on 2026-11-12** (deploy-dev run 43114073852, the scoped deploy role's first
  IAM and EC2 writes): data_platform 3 changes, security 3 changes, foundation and
  observability unchanged. Verified afterwards:
  - `make test-aws`: 27 passed (tenant isolation, exports, admin product), so the operator can
    still assume the tenant and admin roles;
  - `make tenant-exports-run`: succeeded, so the exports state machine can still assume each
    tenant role;
  - the tenant access-check state machine: SUCCEEDED.
- **The repository-wide skips are dev-scope decisions, not oversights.** The table is the list
  to revisit for production.
