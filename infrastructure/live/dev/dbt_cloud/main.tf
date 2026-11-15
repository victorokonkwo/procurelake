# dev / dbt_cloud: a dbt Cloud development environment beside the production runner (ADR 0051).
#
# Production Gold is still built only by the dbt runner on ECS Fargate (ADR 0029). This root
# lets a dbt Cloud (Developer plan) IDE work on the same dbt project against dev Athena:
#   - it reads Silver, and builds every model into its own Glue database, procurelake_dev_dbt_cloud
#     (s3://<lake>/dbt_cloud/), never into Gold, so it cannot touch tenant marts or their
#     Lake Formation grants;
#   - it queries only through its own Athena workgroup (results enforced, KMS-encrypted);
#   - it is an IAM user, because dbt Cloud's Athena connection takes only an access key
#     (docs.getdbt.com "Connect Amazon Athena", read 2026-11-14). The key works only from dbt
#     Cloud's published addresses, and only for the calls below.
#
# Applied by the owner, not by CI: the deploy role is denied iam:CreateUser and
# iam:PutUserPolicy on purpose (ADR 0036). The access key is created by hand after apply
# (docs/runbooks/dbt-cloud.md) and never appears in Terraform state. Removing this root
# (terraform destroy) deletes the user and its keys, the workgroup and the sandbox database.

locals {
  foundation = data.terraform_remote_state.foundation.outputs
  name       = "procurelake-dev-dbt-cloud"

  lake_bucket      = local.foundation.bucket_names["lake"]
  artifacts_bucket = local.foundation.bucket_names["artifacts"]
  lake_arn         = "arn:aws:s3:::${local.lake_bucket}"
  art_arn          = "arn:aws:s3:::${local.artifacts_bucket}"
  silver_db        = local.foundation.silver_database_name
  sandbox_db       = replace(local.name, "-", "_")
  glue             = "arn:aws:glue:${var.region}:${var.aws_account_id}"

  # The athena module writes results under athena_results/<workgroup name>/.
  results_prefix = "athena_results/${local.name}/"
  sandbox_prefix = "dbt_cloud/"
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/foundation/terraform.tfstate"
    region = var.region
  }
}

# ----------------------------------------------------------------------------- catalog
# Outside the Lake Formation-registered gold/ location, so access is IAM-only (like bench/).
resource "aws_glue_catalog_database" "sandbox" {
  name         = local.sandbox_db
  description  = "ProcureLake dev dbt Cloud development builds (from Silver; not tenant-facing, not Gold)."
  location_uri = "s3://${local.lake_bucket}/${local.sandbox_prefix}"
}

module "workgroup" {
  source = "../../../modules/athena"

  name                           = local.name
  description                    = "ProcureLake dev dbt Cloud development queries (results encrypted, scan cutoff)."
  results_bucket_name            = local.artifacts_bucket
  kms_key_arn                    = local.foundation.data_kms_key_arn
  bytes_scanned_cutoff_per_query = var.bytes_scanned_cutoff_per_query
}

# ----------------------------------------------------------------------------- identity
resource "aws_iam_user" "dbt_cloud" {
  # checkov:skip=CKV_AWS_273: a machine identity for dbt Cloud, not a person; dbt Cloud's Athena connection accepts only an access key, no SSO or role (ADR 0051)
  name = local.name
  path = "/procurelake/"

  # The access key is created outside Terraform (runbook); destroy must still remove it.
  force_destroy = true

  tags = { Purpose = "dbt Cloud development connection (ADR 0051)" }
}

resource "aws_iam_user_policy" "dbt_cloud" {
  # checkov:skip=CKV_AWS_40: one user with one purpose; a group would hold only this user. The policy is scoped and IP-restricted (ADR 0051)
  name = "dbt-cloud-development"
  user = aws_iam_user.dbt_cloud.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The key is useless anywhere but dbt Cloud. Calls Athena and S3 make on the user's
        # behalf (reading Silver, writing results) carry aws:ViaAWSService = true and are
        # exempt; dbt-athena's own Glue and S3 calls come from dbt Cloud's addresses.
        Sid      = "DenyOutsideDbtCloud"
        Effect   = "Deny"
        Action   = "*"
        Resource = "*"
        Condition = {
          NotIpAddress = { "aws:SourceIp" = var.dbt_cloud_ip_addresses }
          Bool         = { "aws:ViaAWSService" = "false" }
        }
      },
      {
        Sid    = "QueryInOwnWorkgroup"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:StopQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:GetQueryRuntimeStatistics",
          "athena:GetWorkGroup",
          "athena:ListQueryExecutions",
        ]
        # Built from the name (not the module output) so the policy is known at plan time and
        # can be simulated before apply. Same ARN: modules/athena names the workgroup var.name.
        Resource = "arn:aws:athena:${var.region}:${var.aws_account_id}:workgroup/${module.workgroup.workgroup_name}"
      },
      {
        Sid    = "ReadSilverAndSandboxCatalog"
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetDatabases",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetPartition",
          "glue:GetPartitions",
          "glue:BatchGetPartition",
        ]
        Resource = [
          "${local.glue}:catalog",
          "${local.glue}:database/${local.silver_db}",
          "${local.glue}:table/${local.silver_db}/*",
          "${local.glue}:database/${local.sandbox_db}",
          "${local.glue}:table/${local.sandbox_db}/*",
        ]
      },
      {
        # The calls dbt-athena 1.11.1 makes to build and swap tables, as for the data engineer
        # (modules/iam, data-engineer-build), limited to the sandbox database.
        Sid    = "BuildSandboxTables"
        Effect = "Allow"
        Action = [
          "glue:CreateTable",
          "glue:UpdateTable",
          "glue:DeleteTable",
          "glue:GetTableVersions",
          "glue:DeleteTableVersion",
          "glue:BatchDeleteTableVersion",
        ]
        Resource = [
          "${local.glue}:catalog",
          "${local.glue}:database/${local.sandbox_db}",
          "${local.glue}:table/${local.sandbox_db}/*",
        ]
      },
      {
        Sid      = "LocateBuckets"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = [local.lake_arn, local.art_arn]
      },
      {
        Sid       = "ListSilverAndSandbox"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["silver/*", "${local.sandbox_prefix}*"] } }
      },
      {
        Sid      = "ReadSilver"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${local.lake_arn}/silver/*"
      },
      {
        Sid      = "WriteSandbox"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.lake_arn}/${local.sandbox_prefix}*"
      },
      {
        Sid       = "ListQueryResults"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["${local.results_prefix}*"] } }
      },
      {
        # Athena writes results with multipart uploads (as modules/iam, AthenaResultMultipart).
        Sid      = "QueryResultMultipart"
        Effect   = "Allow"
        Action   = "s3:ListBucketMultipartUploads"
        Resource = local.art_arn
      },
      {
        Sid      = "QueryResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.art_arn}/${local.results_prefix}*"
      },
      {
        # The lake and artifacts buckets are SSE-KMS with the data key; only S3 may use it.
        Sid       = "UseDataKeyThroughS3"
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = local.foundation.data_kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
      },
    ]
  })
}
