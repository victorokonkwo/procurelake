# Architecture diagrams (TerraVision)

Diagrams of the **deployed dev environment** (`ca-central-1`), generated with
[TerraVision](https://github.com/patrickchugh/terravision) 0.56.1 using the official AWS icons.
They show what the Terraform in `infrastructure/live/dev/` creates. The combined diagram was last
updated on 2026-11-14; `../overview.md` describes the architecture in full.

| File | What it is | How it is produced |
|---|---|---|
| `dev-platform.png` / `.svg` | One end-to-end view across the dev roots (foundation, data_platform, security, cdc): sources, Bronze, Silver, dbt and Gold, tenant products and the document path, with four numbered flows (CDC, batch, Gold, documents) | `make diagrams`, from `dev-platform.tvg.json` + `dev-platform.terravision.yml` |
| `dev-foundation.png` | Raw render of `live/dev/foundation` (KMS, the four buckets, Glue catalog, Athena, IAM) | From that root's `terraform plan` (below) |
| `dev-data-platform.png` | Raw render of `live/dev/data_platform` as of 2026-11-10 (both Glue jobs, CDC checkpoint table, logs); not re-rendered since, so it predates the state machines and runners | Same |
| `dev-cdc.png` | Raw render of `live/dev/cdc` (VPC, RDS source, DMS, endpoints, secret) | Same |

## The combined diagram is curated, the per-root ones are not

TerraVision draws one Terraform root at a time, and the dev platform is split across three roots
joined by `terraform_remote_state`. The raw renders are exact, but most of their edges are IAM
policy and KMS references, which hides the data flow. `dev-platform.tvg.json` is a hand-written
graph in TerraVision's [graph format](https://patrickchugh.github.io/terravision/graph-format/):

- Every AWS node is a real resource from one of the dev roots, with a shorter name
  (`aws_glue_job.apply_cdc` is `module.apply_cdc.aws_glue_job.this` in `data_platform`;
  `aws_rds_postgres.source` is `module.source.aws_db_instance.this` in `cdc`).
- Every arrow is a data path set in the Terraform: bucket and prefix arguments, the DMS S3
  target (`bronze/postgres_cdc/`), Glue job arguments (`--raw-bucket`, `--silver-location`,
  `--checkpoint-table`), and the Athena results location (`artifacts/athena_results/`).
- Names are shortened the same way for the newer resources: `aws_ecs_cluster.dbt_runner` and
  `aws_ecs_cluster.task_runner` are `module.dbt_runner` / `module.task_runner`'s
  `aws_ecs_cluster.this` (one module, two instances); `aws_athena_workgroup.tenant` is
  `module.tenant_workgroup` (one per tenant, drawn once) in `security`.
- **dbt** is the dbt runner: dbt-athena in a container on ECS Fargate, started by the
  `dbt_transformation` state machine (by `cdc_processing` for changed tables, and by the daily
  full-build schedule). It builds Gold through the engineering Athena workgroup.
- **Textract and Bedrock** are managed APIs the extraction task calls; the Terraform creates no
  resource for them, so they are drawn as API endpoints (`tv_aws_server`), like the USAspending
  API.
- Left out on purpose: IAM role policies, S3 bucket sub-resources, route tables, security group
  rules, DMS endpoint and task objects (shown as the replication instance), and the Bronze
  catalog table; ECS task definitions and ECR repositories (shown as the cluster); the public
  network the Fargate tasks run in; the `data_quality` and `access_check` state machines, the
  admin role and workgroup, Lake Formation filters and grants (shown as the registered Gold
  location), and CloudWatch alarms and EventBridge alert rules (the alerts topic and dashboard
  are shown). The `bootstrap` root (state bucket, budget, GitHub OIDC roles) is not drawn.
- The RDS instance is single-AZ. Its subnet group spans both public subnets; AZ1 is shown.
- `dev/cdc` is **disposable**. It exists only during CDC sessions (`make cdc-down`).

**When an edited dev root adds, removes or rewires a resource, update `dev-platform.tvg.json` to
match and run `make diagrams`.**

## Regenerating the per-root renders

These need a plan. A plan needs AWS credentials and the S3 backend, but nothing is applied:

```bash
cd infrastructure/live/dev/<root>
terraform plan -out=<root>.tfplan            # or reuse the plan from the last make *-plan
terraform show -json <root>.tfplan > /tmp/<root>.plan.json
terraform graph > /tmp/<root>.graph.dot
uvx --from terravision==0.56.1 terravision draw \
  --planfile /tmp/<root>.plan.json --graphfile /tmp/<root>.graph.dot --source . \
  --format png --outfile /tmp/<root>
```

Do not commit the plan JSON. It contains resolved values, including the account ID.
