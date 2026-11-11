# ADR 0036: Scoped CI deploy role (replaces AdministratorAccess)

- **Status:** Accepted. Applied when the owner applies bootstrap. Verified end to end by the
  first approved CI apply (see Consequences).
- **Date:** 2026-11-11
- **Implementation:** `infrastructure/bootstrap/main.tf` (`local.deploy_statements`,
  `aws_iam_role_policy.github_deploy_dev`, `ReadOnlyAccess` attachment); test
  `deploy_role_is_scoped_not_admin` in `infrastructure/bootstrap/tests/bootstrap.tftest.hcl`.
- **Resolves:** open question 27, and the "deploy role is over-privileged" gap in ADR 0019.

## Context
ADR 0035 made `procurelake-github-deploy-dev` the role that applies four Terraform roots from
GitHub Actions. It had `AdministratorAccess`, limited only by its trust (the GitHub `dev`
environment, with owner approval). Work package G's security checks call for least privilege.

The roots' state on 2026-11-11 lists what the role must manage:
- S3 bucket settings;
- Glue databases, tables and jobs, plus the job code objects;
- IAM workload roles and their inline policies;
- Lake Formation settings, grants, filters and opt-ins;
- Athena workgroups and named queries, Step Functions, EventBridge rules and Scheduler
  schedules;
- SNS, CloudWatch alarms and the dashboard, CloudWatch Logs;
- DynamoDB, ECS, ECR, the KMS data key, and the dbt runner's VPC.

Almost every name starts with `procurelake-dev-` (`procurelake_dev_` for Glue databases). The
exceptions are fixed by AWS or Glue:
- the two DMS roles (`dms-vpc-role`, `dms-cloudwatch-logs-role`);
- the `/aws-glue/jobs/*` log groups;
- generated EC2 and KMS ids.

## Decision
1. **Reads:** the managed `ReadOnlyAccess`, for refresh. The same deny as the plan role covers
   objects: no S3 object may be read, written or deleted except state and
   `artifacts/glue/*` (the Glue code Terraform uploads). The deploy role can change bucket
   settings but never touches lake data.
2. **Writes, scoped by name:** `glue`, `athena`, `states`, `scheduler`, `events`, `sns`,
   `cloudwatch`, `logs`, `dynamodb`, `ecs`, `ecr` and `s3` are allowed only on `procurelake-dev`
   resource ARNs and the fixed exceptions above.
3. **Writes without usable resource names:**
   - `ecs:RegisterTaskDefinition` and `ecs:DeregisterTaskDefinition`;
   - `logs:DescribeLogGroups`;
   - `kms:CreateKey`, and `kms:*` on keys and on `procurelake-*` aliases;
   - `lakeformation:*`;
   - an explicit list of VPC, subnet, route, internet gateway and security group actions.

   EC2 instances and NAT gateways are not allowed.
4. **IAM:** only `procurelake-dev-*` roles and the two DMS roles can be created, changed,
   deleted or passed.
5. **Explicit denies:**
   - **managed policies:** attaching any managed policy except the two DMS service-role
     policies, so AdministratorAccess can't come back by attachment;
   - **new identities:** creating users, access keys, login profiles, OIDC providers or
     customer-managed policies;
   - **CI roles:** any IAM action on the `procurelake-github-*` roles;
   - **the state key:** destructive or policy changes to it and its alias.

## Consequences
- **Verified before applying:** IAM's policy simulator ran 36 representative calls against the
  rendered policy, and every one gave the intended decision. Every write the roots perform was
  allowed. The calls that must fail were denied: AdministratorAccess attachment, the CI roles,
  users, other roles, lake objects, the state bucket, the budget, EC2 instances and NAT.
- **Not verifiable offline:** whether a real apply needs an action this policy misses. The
  first approved CI apply is the test. A miss fails that apply with `AccessDenied` naming the
  action, and the fix is a bootstrap change.
- **Residual risk:**
  - inline `iam:PutRolePolicy` on a `procurelake-dev-*` role can still grant that role broad
    rights;
  - `lakeformation:*` includes changing the data lake admins.

  Both need an approved plan that lists the change. Permissions boundaries on every workload
  role would close the first; they are recorded as a follow-up, not done.
- **The policy is 5,276 characters**, against IAM's 10,240-character limit for an inline role
  policy.
