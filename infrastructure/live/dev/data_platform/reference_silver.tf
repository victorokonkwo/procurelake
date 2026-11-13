# ----------------------------------------------------------------------------- reference -> silver
# ADR 0044 (owner decisions 2026-11-13, open questions 30 and 34): USAspending contract awards in
# Silver as one current-state row per award. The Glue job applies every settled, succeeded
# USAspending batch not yet applied (pending batches come from the manifests; applied ones from
# the job's own run summaries). The batch ingestion state machine starts it after a USAspending
# ingestion or re-sweep. max_concurrent_runs = 1 (glue_spark_job module) keeps one writer.
locals {
  reference_to_silver_job = "${local.name_prefix}-reference-to-silver"
  reference_silver_table  = "silver_usaspending_contract_awards"
  reference_to_silver_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListJobPrefixesRaw"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.raw_arn
        Condition = { StringLike = { "s3:prefix" = ["bronze/usaspending/*", "quarantine/silver/spending_by_award/*"] } }
      },
      {
        Sid       = "ListJobPrefixesLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["silver/${local.reference_silver_table}/*"] } }
      },
      {
        Sid       = "ListJobPrefixesArtifacts"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["pipeline_manifests/usaspending/*", "pipeline_runs/silver/usaspending/*"] } }
      },
      {
        Sid      = "ReadUsaspendingBronze"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${local.raw_arn}/bronze/usaspending/*", "${local.art_arn}/pipeline_manifests/usaspending/*"]
      },
      {
        Sid      = "WriteQuarantineAndSilver"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.raw_arn}/quarantine/silver/spending_by_award/*", "${local.lake_arn}/silver/${local.reference_silver_table}/*"]
      },
      {
        Sid      = "WriteRunSummaries"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.art_arn}/pipeline_runs/silver/usaspending/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "ManageReferenceIcebergTable"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.silver_db}",
          "${local.glue_prefix}:table/${local.silver_db}/${local.reference_silver_table}",
        ]
      },
      {
        Sid      = "DenyBronzeDeletes"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = ["${local.raw_arn}/bronze/*", "${local.raw_arn}/documents/*"]
      },
    ]
  })
}

module "reference_to_silver" {
  source = "../../../modules/glue_spark_job"

  name                    = local.reference_to_silver_job
  description             = "Apply pending USAspending batches from Bronze to the current-state Silver Iceberg table (ADR 0044)."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/reference_to_silver.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.reference_to_silver_policy
  log_group_arns          = [for g in aws_cloudwatch_log_group.glue : g.arn]

  job_arguments = {
    "--datalake-formats" = "iceberg"
    "--conf"             = local.iceberg_conf
    "--contract"         = "usaspending_contract_awards"
    "--artifacts-bucket" = local.buckets["artifacts"]
    "--catalog"          = "glue_catalog"
    "--silver-database"  = local.silver_db
    "--silver-location"  = "s3://${local.buckets["lake"]}/silver"
    "--quarantine-root"  = "s3://${local.buckets["raw"]}/quarantine"
    "--max-batches"      = tostring(var.reference_max_batches)
    "--region"           = var.region
  }
}
