# ADR 0019: Terraform state, bootstrap stack, and CI authentication

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `infrastructure/bootstrap/`, `infrastructure/live/dev/foundation/`

## Context
Sprint 1 Day 3 covers the Terraform backend, AWS authentication, KMS, buckets, IAM, and budget alerts. The plan requires the budget before any persistent infrastructure, no long-lived AWS keys in CI (OIDC), and independent state per root module.

## Decision
1. **Bootstrap stack with local state,** applied once by a human. It creates the cost budget (USD 50/month; alerts at 50/80/100% of actual spend and 100% of forecast), a KMS key for state, the remote-state bucket, and GitHub OIDC federation. Its state can be migrated into the bucket afterwards (see `infrastructure/README.md`).
2. **Remote state:** a single bucket, `procurelake-tfstate-<account>-<region>`, with versioning (superseded versions kept 365 days), SSE-KMS, all public access blocked, ACLs disabled, TLS-only, and `prevent_destroy`. Each root module uses its own key (for example `dev/foundation/terraform.tfstate`). **Locking uses native S3 lock files (`use_lockfile = true`)**, because HashiCorp's S3 backend documentation marks DynamoDB locking as deprecated ([S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)). This needs Terraform 1.11 or later.
3. **Versions:** Terraform `>= 1.11, < 2.0` (1.17.1 used locally) and AWS provider `>= 6.72, < 7.0` (6.72.0 was the registry's latest at the time of writing). Lock files are committed.
4. **CI authentication:** a GitHub OIDC provider with two roles:
   - `procurelake-github-plan`: trusts pull requests and pushes to `dev` only. It has `ReadOnlyAccess` plus state access, and is explicitly **denied reading any S3 object except state**, so CI can never read lake data.
   - `procurelake-github-deploy-dev`: trusts only workflows running in the GitHub `dev` **environment**. It has `AdministratorAccess` for now (a known gap; see Consequences).
5. **Account guard:** every provider sets `allowed_account_ids`.
6. **Bucket naming:** `<prefix>-<role>-<account>-<region>`, for global uniqueness without random suffixes.
7. **Lakehouse buckets** (raw, lake, products, artifacts): versioned, SSE-KMS with a dedicated data key and bucket keys enabled, private, TLS-only. Current objects in Bronze **never expire**. Noncurrent Bronze versions are kept 365 days; other buckets keep them 30 days. Athena results expire after 30 days.
8. **Workload roles:** `ingestion` (writes only the Bronze landing prefixes and manifests) and `data_engineer` (reads Bronze; writes lake, products, artifacts). Both are **explicitly denied** `s3:DeleteObject` and `s3:DeleteObjectVersion` on Bronze.
9. **Offline verification:** `terraform test` with `mock_provider "aws"` checks the security properties (21 runs) without credentials. Each check was confirmed to fail when its control was deliberately weakened.

## Consequences
- **Bronze immutability is partial.** It relies on versioning plus the IAM delete denial. Account administrators can still delete. S3 Object Lock would be stronger, but it must be enabled when a bucket is created and complicates teardown. Reconsider it before any production use.
- **The deploy role is over-privileged** until the Week 10 security review replaces `AdministratorAccess` with a scoped policy.
- **Not deployed.** No AWS credentials were available on 2026-11-10. The Day 3 completion gate ("`terraform plan` succeeds; foundation deploys") is **not met** until someone runs plan and apply against a real account.
