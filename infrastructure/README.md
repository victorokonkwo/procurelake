# Infrastructure

Terraform for ProcureLake (ADR 0010, 0019). Region: `ca-central-1`. Only `dev` is deployed (ADR 0011).

```
infrastructure/
├── bootstrap/                 # applied once by a human: budget, state bucket, GitHub OIDC roles
├── modules/                   # reusable building blocks (each has offline tests in tests/)
│   ├── budgets/  kms/  s3_lakehouse/  iam/  glue_catalog/  athena/  glue_spark_job/
│   ├── dms_account_roles/  cdc_network/  rds_postgresql/  dms_s3_cdc/
└── live/dev/
    ├── foundation/            # data KMS key, 4 lakehouse buckets, workload roles,
    │                          #   Bronze/Silver/Gold Glue databases, engineering Athena workgroup
    ├── data_platform/         # Glue jobs (Bronze -> Silver), reading foundation's remote state
    └── cdc/                   # DISPOSABLE, hourly-billed: VPC, RDS PostgreSQL source, AWS DMS
                               #   (destroy after each session: make cdc-down; ADR 0027)
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

5. **dev data_platform** (after foundation; ADR 0025):
   ```bash
   cp infrastructure/live/dev/foundation/backend.hcl infrastructure/live/dev/data_platform/backend.hcl
   cp infrastructure/live/dev/data_platform/terraform.tfvars.example infrastructure/live/dev/data_platform/terraform.tfvars
   make dev-data-platform-plan               # builds the wheel, then plans; review
   terraform -chdir=infrastructure/live/dev/data_platform apply data_platform.tfplan
   make silver-run                           # one Glue run on the latest complete snapshot batch
   ```

## Current deployment (dev)

Region `ca-central-1`, deployed 2026-11-10 to the dev account (its ID is in the git-ignored
tfvars). Both stacks' state is in `s3://procurelake-tfstate-<account>-ca-central-1/` under
`bootstrap/`, `dev/foundation/` and `dev/data_platform/` (added 2026-11-10: the Silver Glue job).

## CI deploys (GitHub Actions, ADR 0035)

`.github/workflows/deploy-dev.yml` plans and applies `foundation`, `data_platform`, `security`
and `observability`, in that order. Bootstrap and `dev/cdc` stay manual.

- **Pull request into `dev`:** plan with `procurelake-github-plan` (read-only). The job summary
  lists each changed resource's address and action, never attribute values: the repository and
  its logs are public. The account ID and state key are secrets, so GitHub masks them.
- **Push to `dev`:** the same plan. If anything changes, the `apply` job waits in the GitHub
  environment `dev` for the owner's approval. It then re-plans with
  `procurelake-github-deploy-dev` and applies each root only if its fingerprint (actions,
  addresses and a hash of the planned values) equals the one approved. If an earlier root's
  apply changes what a later root plans, the job stops before that root: re-run the workflow
  and review the new plan.
- **Inputs:**
  - secrets `AWS_ACCOUNT_ID` and `TF_STATE_KMS_KEY_ARN`;
  - variables `DBT_IMAGE_DIGEST` and `DATA_LAKE_OPERATOR`.

  After `make dbt-image`, update the digest, otherwise CI keeps deploying the previous image:
  `gh variable set DBT_IMAGE_DIGEST --body "$(cat build/dbt_runner.digest)"`. An empty digest
  fails the job rather than planning the removal of the state machines.
- **Local applies still work.** Keep the security `terraform.tfvars` admin lists equal to CI's:
  the data lake settings resource replaces the whole list.

## Safety properties

- `allowed_account_ids` on every provider stops Terraform from acting on any other account.
- The state bucket has `prevent_destroy`. Lakehouse buckets have `force_destroy = false`, so
  destroying a non-empty bucket fails instead of deleting data.
- State locking uses native S3 lock files (`use_lockfile = true`). DynamoDB-based locking is
  deprecated in the S3 backend.
- Budget alerts are notifications, not spending caps.
- `*.tfvars` and `backend.hcl` are git-ignored because they contain the account ID and alert emails.

## Known gaps

- The GitHub `procurelake-github-deploy-dev` role is scoped (ADR 0036): `ReadOnlyAccess` plus writes limited to
  `procurelake-dev` resources. Inline role policies and `lakeformation:*` remain broad; permissions
  boundaries are a follow-up.
- Checkov runs in `ci-security` (ADR 0037). Its repository-wide skips in `.checkov.yaml` are dev-scope
  choices (no replication, access logs, flow logs or customer-managed log keys) to revisit before production.
