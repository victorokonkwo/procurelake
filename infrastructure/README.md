# Infrastructure

Terraform for ProcureLake (ADR 0010, 0019). Region: `ca-central-1`. Only `dev` is deployed (ADR 0011).

```
infrastructure/
├── bootstrap/                 # applied once by a human: budget, state bucket, GitHub OIDC roles
├── modules/                   # reusable building blocks (each has offline tests in tests/)
│   ├── budgets/  kms/  s3_lakehouse/  iam/  glue_catalog/  athena/
└── live/dev/foundation/       # dev root: data KMS key, 4 lakehouse buckets, workload roles,
                               #   Bronze Glue catalog, engineering Athena workgroup
```

Other module and root directories from `docs/architecture/repository_structure.md` are added when their sprint starts.

## Offline checks (no AWS credentials, nothing created)

```bash
make tf-fmt        # formatting
make tf-validate   # init -backend=false + validate for every module and root
make tf-test       # terraform test with mock_provider "aws": asserts encryption, public-access
                   # blocks, TLS-only policies, Bronze delete denial, OIDC trust conditions, budget
```

## First deployment (manual, in order)

Prerequisites: Terraform >= 1.11, AWS CLI v2, and credentials for the target account. Use an
IAM Identity Center session if you can (`aws configure sso`), not long-lived access keys.

1. **Bootstrap.** This creates the budget first, then the state bucket and the GitHub OIDC roles.
   ```bash
   cp infrastructure/bootstrap/terraform.tfvars.example infrastructure/bootstrap/terraform.tfvars
   # set aws_account_id and budget_alert_emails
   make bootstrap-plan                       # review the plan output carefully
   terraform -chdir=infrastructure/bootstrap apply bootstrap.tfplan
   ```
   AWS Budgets emails each subscriber. Confirm the alert email arrives.

2. **Backend config for the live roots:**
   ```bash
   terraform -chdir=infrastructure/bootstrap output -raw backend_hcl \
     > infrastructure/live/dev/foundation/backend.hcl
   cp infrastructure/live/dev/foundation/terraform.tfvars.example infrastructure/live/dev/foundation/terraform.tfvars
   ```

3. **dev foundation:**
   ```bash
   make dev-foundation-plan                  # review
   terraform -chdir=infrastructure/live/dev/foundation apply foundation.tfplan
   ```

4. **Move the bootstrap state into the state bucket.** `bootstrap/main.tf` already declares
   `backend "s3" { key = "bootstrap/terraform.tfstate" }` (it was migrated on 2026-11-10). To work
   on bootstrap from a fresh clone, run
   `terraform -chdir=infrastructure/bootstrap init -backend-config=../live/dev/foundation/backend.hcl`.
   For a brand-new account, comment out the backend block for the first apply, then run
   `init -migrate-state` with the same flag.

## Current deployment (dev)

Region `ca-central-1`, deployed 2026-11-10 to the dev account (its ID is in the git-ignored
tfvars). Both stacks' state is in `s3://procurelake-tfstate-<account>-ca-central-1/` under
`bootstrap/` and `dev/foundation/`.

## Safety properties

- `allowed_account_ids` on every provider stops Terraform from acting on any other account.
- The state bucket has `prevent_destroy`. Lakehouse buckets have `force_destroy = false`, so
  destroying a non-empty bucket fails instead of deleting data.
- State locking uses native S3 lock files (`use_lockfile = true`). DynamoDB-based locking is
  deprecated in the S3 backend.
- Budget alerts are notifications, not spending caps.
- `*.tfvars` and `backend.hcl` are git-ignored because they contain the account ID and alert emails.

## Known gaps

- The GitHub `procurelake-github-deploy-dev` role uses `AdministratorAccess`, restricted to the
  GitHub `dev` environment through its trust policy. It will be replaced with a scoped policy in the
  Week 10 security review (`docs/open-questions.md`).
- There's no Terraform linter or security scanner in the toolchain yet (`docs/open-questions.md` item 5).
