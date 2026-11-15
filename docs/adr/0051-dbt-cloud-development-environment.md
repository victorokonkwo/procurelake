# ADR 0051: dbt Cloud as a development environment beside the production runner

- **Status:** Accepted (owner decision 2026-11-14). Being applied 2026-11-15: the workgroup, the
  sandbox database and the user exist; the first two applies failed on IAM limits the mock
  provider can't check (tag characters, then the inline policy size), fixed in PRs #29 and
  this one. The policy was checked with the IAM policy simulator before apply. Connecting dbt
  Cloud is still to do (`docs/runbooks/dbt-cloud.md`).
- **Date:** 2026-11-14
- **Implementation:**
  - Terraform: `infrastructure/live/dev/dbt_cloud/` (owner-applied root; `make dev-dbt-cloud-plan`)
  - Policy check: `src/procurelake/governance/dbt_cloud_access.py`, `dbt-cloud-check` in
    `src/procurelake/governance/cli.py` (`make dbt-cloud-check`)
  - Tests: `infrastructure/live/dev/dbt_cloud/tests/dbt_cloud.tftest.hcl`,
    `tests/unit/governance/test_dbt_cloud_access.py`
  - Runbook: `docs/runbooks/dbt-cloud.md`

## Context
The owner asked whether dbt Cloud (the dbt platform) could replace the dbt runner on ECS Fargate
(ADR 0029). The dbt platform docs (read 2026-11-14) show:
- **Credentials.** The Athena connection takes an IAM access key ID and secret access key. An
  `aws_session_token` can be added, but those temporary credentials expire and must be refreshed
  by hand. There is no IAM role, external ID or OIDC option.
- **Region.** The plan open to every account (and the free Developer plan) runs in AWS us-east-1
  (`ACCOUNT_PREFIX.us1.dbt.com`). No dbt platform region is in Canada.
- **Versions.** dbt Cloud environments use dbt release tracks, not an exact dbt-core and
  dbt-athena pin.

Replacing the runner would have put a long-lived key behind every production Gold build
(AGENTS.md §2.7), moved query orchestration out of Canada, and lost the dbt pins of ADR 0026. It
would also have changed the selective CDC builds of ADR 0050 and needed new freshness evidence.

## Decision
1. **Production Gold stays on the dbt runner** (ECS Fargate, Step Functions, the data-engineer
   role, ADR 0029 and ADR 0050). Nothing in the pipeline calls dbt Cloud.
2. **dbt Cloud is a development environment only.** A dbt Cloud project (Developer plan) uses
   this repository's `dbt/` directory and the dev Athena connection. It is used for the IDE,
   docs and lineage. Its runs are not evidence for any gate.
3. **It builds only into a sandbox.** It reads Silver (`procurelake_dev_silver`) and builds every
   model into `procurelake_dev_dbt_cloud` (`s3://<lake>/dbt_cloud/`). That location is outside the
   Lake Formation-registered `gold/`, so it cannot change Gold, the tenant marts or their grants.
   It queries only through its own workgroup, `procurelake-dev-dbt-cloud` (results enforced
   under `athena_results/procurelake-dev-dbt-cloud/`, KMS-encrypted, 1 GiB scan cutoff).
4. **A dedicated IAM user, `procurelake-dev-dbt-cloud`**, because the connection takes only an
   access key. Its policy (customer managed and attached to the user: at about 3,400 characters
   it is over the 2,048-character limit on a user's inline policies):
   - denies every direct call whose source address is not one of dbt Cloud's published us-east-1
     addresses (`aws:SourceIp`, with `aws:ViaAWSService` false, so that Athena and S3 can still act
     for it). A leaked key does nothing from anywhere else;
   - allows Athena in its own workgroup only, Glue reads on Silver and the sandbox, Glue table
     writes on the sandbox only, S3 reads under `silver/`, writes under `dbt_cloud/` and its
     results prefix, and the data key only through S3 (`kms:ViaService`);
   - has no Gold, Bronze, products bucket, IAM or database-creation rights.
5. **Owner-applied, key made by hand.** The root is not in CI: the deploy role is denied
   `iam:CreateUser` and `iam:PutUserPolicy` on purpose (ADR 0036). The access key is created with
   the AWS CLI after apply, pasted into dbt Cloud and never stored in Git or Terraform state.
   Rotate it every 90 days. `terraform destroy` of the root removes the user and its keys.
6. **Data leaving Canada.** dbt Cloud receives compiled SQL, query metadata and the rows a
   developer previews. All dev data is synthetic or public (AGENTS.md §2.3), so this is
   acceptable in dev. It is not acceptable for real tenant data: a staging or production dbt
   Cloud connection needs a new ADR.

## Alternatives considered
- **Replace the runner with dbt Cloud jobs** (Step Functions calling the dbt Cloud API): rejected
  for the reasons in Context.
- **Temporary credentials (`aws_session_token`)**: no long-lived key, but they expire within
  hours and must be pasted again by hand. Not chosen: unusable for a working IDE.
- **Build into the real Gold database from dbt Cloud**: rejected. A dbt Cloud `table` rebuild of
  a mart would drop it and its Lake Formation grants (ADR 0030).
- **dbt Core in a local IDE only** (already possible with `make dbt-build`): kept. dbt Cloud is
  in addition to it, not instead.

## Consequences
- Two dbt environments: production (pinned, in Canada, role-based) and dbt Cloud (release track,
  us-east-1, key-based). A dbt Cloud success does not prove the pinned build passes. CI's
  `make dbt-compile` and the runner remain the checks.
- dbt Cloud's addresses can change (its docs say so). dbt Cloud then gets AccessDenied until
  `dbt_cloud_ip_addresses` is updated and re-applied.
- `make dbt-cloud-check` (IAM policy simulator, read-only) shows the policy still gives the
  expected decisions: 22 cases, run before apply against the planned policy on 2026-11-14,
  all as expected.
- The sandbox database and the `dbt_cloud/` prefix hold copies of Gold built from dev Silver.
  They are synthetic and public data, not tenant products; nothing grants tenants access.
- The free plan's monthly allowance of successful model builds (3,000 on the pricing page read
  2026-11-14) limits a full build to about 150 runs a month at 19 models.
