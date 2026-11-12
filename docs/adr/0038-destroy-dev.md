# ADR 0038: `make destroy-dev`, a safeguarded teardown of the dev compute roots

- **Status:** Accepted (dry run verified against live state; not executed)
- **Date:** 2026-11-12
- **Implementation:**
  - `src/procurelake/operations/teardown.py`: what is kept and destroyed, plan verification,
    active-work check, DynamoDB backup
  - `scripts/aws/destroy_dev.py` and `make destroy-dev [DRY_RUN=1]`
  - Tests: `tests/unit/operations/test_teardown.py`
- **Owner choice (2026-11-12):** tear down the compute roots; keep Terraform state, the
  foundation, the DynamoDB tables, the image repository and the logs.

## Context
Work package G asks for `make destroy-dev` "with safeguards", one that must not destroy
Terraform state or essential audit artifacts. The dev environment has six Terraform roots:
- `bootstrap`: the state bucket and the CI role;
- `foundation`: lake buckets, KMS, Glue databases, the budget;
- `data_platform`: Glue jobs, Step Functions, the dbt runner, DynamoDB, SNS;
- `security`: Lake Formation and the tenant roles;
- `observability`: alarms and the dashboard;
- `cdc`: RDS and DMS, already disposable through `make cdc-down`.

When dev sits idle for weeks, the recurring costs are the daily maintenance schedule (about
$1.50/month at list price, `docs/benchmarks/2026-11-12-cost-review.md`), the alarms and the
dashboard. The lake data, state and audit trail must survive the teardown.

## Decision
1. **Roots and order:** `cdc` → `observability` → `security` → `data_platform`.
   - The earlier roots read the later ones through remote state, so dependents go first.
   - `bootstrap` and `foundation` are refused outright (`check_roots`). The lake buckets,
     their KMS key, the Glue databases, the Iceberg data and the budget all live there.
2. **Kept inside the destroyed roots:**
   - `aws_dynamodb_table.*`: CDC checkpoints and leases, and AI review decisions. Both have
     deletion protection and PITR;
   - the dbt runner's `aws_ecr_repository` and its lifecycle policy;
   - every `aws_cloudwatch_log_group`: Glue job output and errors, and the dbt runner.

   They hold state or evidence and cost nothing idle beyond storage.
3. **Targeted plan, then verification.** Terraform 1.17.1 has no `-exclude`.
   - For each root, the script lists the managed resources in state (data sources are
     skipped), drops the kept ones, and plans `terraform plan -destroy -target=…` for every
     other resource into a saved plan.
   - The saved plan is read back with `terraform show -json`. `verify_destroy_plan` refuses
     it if it would create or update anything, or delete any kept resource.
   - The saved plan, not a fresh one, is what gets applied.
4. **Refusals before anything is destroyed:**
   - any `procurelake-dev-*` Glue job run in STARTING, RUNNING, STOPPING or WAITING;
   - any `procurelake-dev-*` Step Functions execution still RUNNING.

   A teardown must never cut through a CDC apply that holds a lease.
5. **Backup:** before the first destroy, every table named by a `*_table_name` output of
   `data_platform` is scanned with consistent reads. Each is written as DynamoDB-JSON lines to
   `s3://<artifacts>/teardown/<UTC timestamp>/<table>.jsonl`. The artifacts bucket is in
   `foundation`, which is kept.
6. **Typed confirmation:** the operator must type `destroy procurelake-dev`. Anything else
   discards the saved plans and exits. `DRY_RUN=1` plans and verifies every root, then
   discards the plans without backing up or destroying anything.

## Verification (2026-11-12, dry run against live state)
`make destroy-dev DRY_RUN=1` (evidence: `build/evidence/destroy_dev_dry_run.txt`, local):

| Root | To destroy | Kept |
|---|---|---|
| `cdc` | 0 (down at the time) | 0 |
| `observability` | 9 | 0 |
| `security` | 57 | 0 |
| `data_platform` | 60 | 7: two Glue log groups, `ai_review`, `cdc_checkpoints`, the dbt log group, the ECR repository and its lifecycle policy |

Every plan passed verification: 126 deletes, no creates or updates, no kept resource
deleted. The real teardown has not been run.

## Consequences
- **Restore:** push or re-run the `deploy-dev` workflow (ADR 0035), or run the four roots'
  plans and approved applies in reverse order (`data_platform`, `security`, `observability`,
  then `cdc` if needed).
  - The kept resources are still in state, so the re-apply only recreates the rest.
  - Iceberg tables and their data are untouched, because they live in `foundation` buckets
    and Glue databases.
- **What a teardown takes away until restore:**
  - **Security:** the Lake Formation settings, the Gold registration, the row filters and
    grants, and the tenant and admin roles. Tenants cannot query at all; access is not opened
    up.
  - **data_platform:** the SNS topic. Its email subscription must be confirmed again after
    the re-deploy.
  - **Both:** the daily maintenance and tenant-export schedules stop, so Iceberg snapshots
    are not expired in the meantime.
- **Not covered:** the KMS keys ($1/month each, list price) and lake storage keep billing.
  This is deliberate, because removing them would destroy the data.
- **Backups:** the DynamoDB backups are copies for audit. The tables themselves are kept, so
  a restore does not reload them.

## Alternatives considered
- **`terraform destroy` per root:** rejected. It would delete the checkpoint and review
  tables, the image repository and the logs. The tables' deletion protection would make it
  fail partway instead.
- **Moving the kept resources into `foundation` first:** cleaner long term, but a state move
  of live resources for little gain now. Recorded as a possible follow-up.
- **Destroying `foundation` with an emptied-bucket safeguard:** rejected. It is the lake and
  the audit trail.
