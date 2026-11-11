# ADR 0035: CI deploys: plan on pull requests, approved apply on dev (GitHub Actions + OIDC)

- **Status:** Accepted. The pull-request plan has run (2026-11-11). The approved apply runs on
  the first merge that changes infrastructure.
- **Date:** 2026-11-11
- **Implementation:**
  - Workflow: `.github/workflows/deploy-dev.yml`
  - Driver: `scripts/ci/terraform_dev.sh`
  - Summary and fingerprint: `scripts/ci/tf_plan_summary.py` (tests:
    `tests/unit/test_tf_plan_summary.py`)
  - Bootstrap: the plan role may read `artifacts/glue/*`
  - Security root: Lake Formation `admins` and `read_only_admins`
  - Docs: `infrastructure/README.md` ("CI deploys")
- **Builds on:** ADR 0019, which created the OIDC provider and both roles but no workflow used
  them.

## Context
Work package G: deploys use GitHub Actions with AWS OIDC and no long-lived keys. Changes need
review, passing tests and a Terraform plan before deployment. Until now every apply ran from the
owner's machine.

Constraints found while designing it:
- **The repository is public.** Actions logs, job summaries and artifacts are readable by
  anyone. A plan file holds every planned value, so it must never be uploaded. Full plan output
  would publish policy JSON, ARNs and bucket names.
- **The plan role could not plan data_platform.** It was denied `s3:GetObject` outside the
  state bucket, so it could not refresh the Glue scripts and wheels Terraform manages in the
  artifacts bucket.
- **Lake Formation:** applying the security root's grants needs a data lake admin. Planning
  them reliably needs a principal that can see every grant: read-only IAM access to Lake
  Formation is not enough to list them all. The settings resource also *replaces* the admin
  lists, so local and CI inputs must agree.
- **The dbt runner image digest existed only locally** (`build/dbt_runner.digest`), and an empty
  digest plans the removal of the state machines.

The owner decided on 2026-11-11:
- **Scope:** four roots, plan and apply.
- **Gate:** a GitHub environment `dev` with the owner as required reviewer.
- **Public logs:** a summary only, with the account ID masked.
- **dbt image:** the digest comes from a repository variable.

## Decision
1. **Roots and order:** foundation, data_platform, security, observability. Bootstrap (the
   state and OIDC themselves) and the disposable `dev/cdc` stack are never run from CI.
2. **Pull requests into `dev`:** plan with `procurelake-github-plan`.
   - Fork pull requests are skipped, since they get no OIDC token or secrets.
   - The job summary lists, per root, each changed resource's address and action (create,
     update, replace, delete, forget). No values are shown.
3. **Pushes to `dev`:** the same plan. If any root changes, the `apply` job runs in the
   environment `dev`:
   - It waits for the owner's approval, and only the `dev` branch may deploy.
   - Only this context can assume `procurelake-github-deploy-dev`.
   - It re-plans each root in order and applies only if that root's **fingerprint** equals the
     approved one. The fingerprint is SHA-256 over the sorted (action, address, hash of
     `after`/`after_unknown`/`after_sensitive`) list.
   - So a different update to the same resource does not pass, and nothing is applied that the
     reviewer did not see listed. With no plan file to hand over, this is what ties the apply
     to the reviewed plan.
4. **Inputs:** the secrets `AWS_ACCOUNT_ID` and `TF_STATE_KMS_KEY_ARN`, which GitHub masks in
   logs, and the variables `DBT_IMAGE_DIGEST` and `DATA_LAKE_OPERATOR`. The driver writes
   `backend.hcl` and `terraform.tfvars` on the runner. An empty digest fails the job.
5. **Plan role read access:** state, plus `artifacts/glue/*`, the deployed Glue scripts and
   wheels built from this public repository. Every other S3 object stays denied.
6. **Lake Formation:**
   - the deploy role joins the operator as a data lake admin, so it can apply the grants;
   - the plan role is a **read-only admin**, so it sees every grant and can change none;
   - the operator's local tfvars carry the same lists.
7. **One pipeline at a time:** concurrency group `deploy-dev`, never cancelled mid-apply, on
   top of the S3 state locks.

8. **The OIDC subject is GitHub's immutable form.** It is
   `repo:victorokonkwo@42738051/procurelake@1452285010:…` (owner and repository IDs), as
   reported by `gh api repos/<owner>/<name>/actions/oidc/customization/sub`. The first PR plan
   failed `AssumeRoleWithWebIdentity` because both role trusts expected
   `repo:victorokonkwo/procurelake:…`. The prefix is now bootstrap variable
   `github_oidc_subject_prefix`. It is stricter than the name form, because a renamed or
   re-created repository with the same name does not match.

## Consequences
- **Upstream changes stop the apply.** If an earlier root's apply changes what a later root
  plans (for example, new foundation outputs), the fingerprint check stops before that root,
  after the earlier roots were applied. A re-run shows the new plan for approval. This is
  deliberate: safe, but a two-approval deploy in that case.
- **The deploy role keeps `AdministratorAccess`** (known gap, ADR 0019), now limited by owner
  approval and the `dev` branch policy. Scoping it is part of the security checks.
- **Reviewers see addresses and actions, not values.** For a value-level review, the owner runs
  the same plan locally (`make dev-*-plan`).
- **The dbt image digest is manual.** It must be updated with `gh variable set` after each
  `make dbt-image`, otherwise CI keeps the previous image.
- **First pull-request plan (PR #3, run 43086259837, 2026-11-11):** foundation, data_platform,
  security and observability all showed **no changes** against the deployed dev. So:
  - a wheel built on the Linux runner has the same SHA-256 as the local build, and the Glue
    jobs do not churn;
  - the plan role, as a Lake Formation read-only admin, sees every grant.
- **Two attempts failed first, both fixed:**
  - the OIDC subject format (Decision 8);
  - `lakeformation:ListLakeFormationOptIns`, which the managed `ReadOnlyAccess` policy lacks,
    is now granted to the plan role.
- **Logs:** in those runs the account ID appeared as `***`, as intended.
