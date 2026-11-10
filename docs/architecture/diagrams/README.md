# Architecture diagrams (TerraVision)

Diagrams of the **deployed dev environment** (`ca-central-1`), generated with
[TerraVision](https://github.com/patrickchugh/terravision) 0.56.1 using the official AWS icons.
They show what the Terraform in `infrastructure/live/dev/` creates. For the target architecture
(Lake Formation, Step Functions, Bedrock and the rest), see `../overview.md`.

| File | What it is | How it is produced |
|---|---|---|
| `dev-platform.png` / `.svg` | One end-to-end view across all three dev roots: sources, Bronze, Silver, Gold, with numbered data flows | `make diagrams`, from `dev-platform.tvg.json` + `dev-platform.terravision.yml` |
| `dev-foundation.png` | Raw render of `live/dev/foundation` (KMS, the four buckets, Glue catalog, Athena, IAM) | From that root's `terraform plan` (below) |
| `dev-data-platform.png` | Raw render of `live/dev/data_platform` (both Glue jobs, CDC checkpoint table, logs) | Same |
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
- Left out on purpose: IAM role policies, S3 bucket sub-resources, route tables, security group
  rules, DMS endpoint and task objects (shown as the replication instance), and the Bronze
  catalog table. The `bootstrap` root (state bucket, budget, GitHub OIDC roles) is not drawn.
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
