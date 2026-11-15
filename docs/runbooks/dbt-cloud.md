# Runbook: dbt Cloud development environment (ADR 0051)

dbt Cloud is a **development environment only**. Production Gold is built by the dbt runner on
ECS Fargate (ADR 0029). dbt Cloud reads dev Silver and builds into the sandbox database
`procurelake_dev_dbt_cloud`, never into Gold.

Connection fields are from the dbt platform docs, "Connect Amazon Athena" (read 2026-11-14).

## 1. Create the AWS side (owner, once)

The deploy role is not allowed to create IAM users (ADR 0036), so the owner applies this root
with their own administrator credentials. It creates 4 free resources: the IAM user, its inline
policy, the Athena workgroup and the Glue database. Queries are billed per byte scanned, as for
any Athena query.

```bash
cp infrastructure/live/dev/foundation/backend.hcl infrastructure/live/dev/dbt_cloud/backend.hcl
cp infrastructure/live/dev/dbt_cloud/terraform.tfvars.example infrastructure/live/dev/dbt_cloud/terraform.tfvars
# set aws_account_id and state_bucket
make dev-dbt-cloud-plan                       # review: 4 to add
terraform -chdir=infrastructure/live/dev/dbt_cloud apply dbt_cloud.tfplan
make dbt-cloud-check                          # IAM policy simulator; must print "passed": true
```

## 2. Create the access key (owner)

The key is never in Terraform or Git. Create it, paste it into dbt Cloud, and don't save it
anywhere else:

```bash
aws iam create-access-key --user-name procurelake-dev-dbt-cloud \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text
```

The key works only from dbt Cloud's us-east-1 addresses. From your own machine every call is
denied. That is expected, and it is the control that makes a stored key acceptable.

## 3. Connect dbt Cloud

1. Sign up for the dbt platform Developer plan. It is hosted in us-east-1
   (`ACCOUNT_PREFIX.us1.dbt.com`), the region whose addresses the policy allows.
2. Create a project from this GitHub repository, with project subdirectory `dbt`.
3. Create an Amazon Athena connection with the values from
   `terraform -chdir=infrastructure/live/dev/dbt_cloud output dbt_cloud_connection`:

   | Field | Value |
   |---|---|
   | AWS region name | `ca-central-1` |
   | Database (catalog) | `awsdatacatalog` |
   | AWS S3 staging directory | `s3_staging_dir` from the output |
   | Athena workgroup | `procurelake-dev-dbt-cloud` |
   | AWS S3 data directory | `s3_data_dir` from the output (`s3://<lake>/dbt_cloud/`) |
   | AWS S3 data naming convention | `schema_table_unique` |

4. Development credentials: the access key ID and secret from step 2, schema
   `procurelake_dev_dbt_cloud`, threads 4.
5. Choose a release track. Production pins dbt-core 1.12.6 and dbt-athena 1.11.1 (ADR 0026); dbt
   Cloud may run a different version, so a dbt Cloud success is not evidence for the pinned build.

The project's `profiles.yml` is not used by dbt Cloud. The sources' Silver schema defaults to
`procurelake_dev_silver`, so no environment variable is needed.

## 4. Check it works

In the dbt Cloud IDE, run `dbt build`. The models appear in `procurelake_dev_dbt_cloud`. Gold
(`procurelake_dev_gold`) does not change, because the user has no rights on it.

## Problems

- **AccessDenied on every call:** dbt Cloud's addresses may have changed (its docs say they can).
  Compare `dbt_cloud_ip_addresses` in `infrastructure/live/dev/dbt_cloud/variables.tf` with the
  dbt docs page "Access, Regions, & IP addresses", update the list, plan, apply, then run
  `make dbt-cloud-check`.
- **AccessDenied on one table or prefix:** the model is reading something other than Silver. Only
  Silver is readable, by design.
- **`make dbt-cloud-check` fails:** the policy no longer gives the decisions in
  `src/procurelake/governance/dbt_cloud_access.py`. Don't use the key until it passes.

## Rotate the key (every 90 days) or revoke it

```bash
aws iam list-access-keys --user-name procurelake-dev-dbt-cloud
aws iam create-access-key --user-name procurelake-dev-dbt-cloud   # paste the new key into dbt Cloud
aws iam update-access-key --user-name procurelake-dev-dbt-cloud --access-key-id <old> --status Inactive
aws iam delete-access-key --user-name procurelake-dev-dbt-cloud --access-key-id <old>
```

If the key may have leaked, delete it at once. To remove dbt Cloud access entirely, run
`terraform -chdir=infrastructure/live/dev/dbt_cloud destroy`. It deletes the user, its keys, the
workgroup and the sandbox database's catalog entry. The tables' files under `dbt_cloud/` remain;
remove them with `aws s3 rm --recursive s3://<lake>/dbt_cloud/`.
