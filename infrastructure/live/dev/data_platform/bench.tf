# Week 10 benchmarks (ADR 0034): two on-demand Glue jobs over the bench databases only.
#
# * bench Silver: the Silver job (same code and wheel) applying `postgres_bench` snapshot
#   batches from Bronze into procurelake_dev_bench_silver at s3://<lake>/bench/silver/.
# * partition evolution: ADD PARTITION FIELD (+ optional rewrite) on bench Silver tables.
#
# Neither role can read or write the dev Silver or Gold, and neither is scheduled. Idle Glue jobs
# cost nothing; each run is billed per DPU-second.

locals {
  bench_silver_db = local.foundation.bench_silver_database_name
  bench_source    = "postgres_bench"
  bench_conf      = replace(local.iceberg_conf, "warehouse=s3://${local.buckets["lake"]}/silver/", "warehouse=s3://${local.buckets["lake"]}/bench/silver/")

  bench_silver_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListBenchRaw"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.raw_arn
        Condition = { StringLike = { "s3:prefix" = ["bronze/${local.bench_source}/*", "quarantine/bench/*"] } }
      },
      {
        Sid       = "ListBenchLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["bench/silver/*"] } }
      },
      {
        Sid       = "ListBenchArtifacts"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["pipeline_manifests/${local.bench_source}/*", "pipeline_runs/silver/${local.bench_source}/*"] } }
      },
      {
        Sid      = "ReadBenchSnapshots"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${local.raw_arn}/bronze/${local.bench_source}/*", "${local.art_arn}/pipeline_manifests/${local.bench_source}/*"]
      },
      {
        Sid      = "WriteBenchQuarantineAndSilver"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.raw_arn}/quarantine/bench/*", "${local.lake_arn}/bench/silver/*"]
      },
      {
        Sid      = "WriteBenchRunSummaries"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.art_arn}/pipeline_runs/silver/${local.bench_source}/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "ManageBenchSilverTables"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.bench_silver_db}",
          "${local.glue_prefix}:table/${local.bench_silver_db}/*",
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

  partition_evolution_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListBenchLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["bench/silver/*"] } }
      },
      {
        Sid      = "RewriteBenchSilverFiles"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = "${local.lake_arn}/bench/silver/*"
      },
      {
        Sid      = "WriteReports"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${local.art_arn}/benchmarks/partition_evolution/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "EvolveBenchSilverTables"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.bench_silver_db}",
          "${local.glue_prefix}:table/${local.bench_silver_db}/*",
        ]
      },
      {
        Sid      = "NoRawAccess"
        Effect   = "Deny"
        Action   = "s3:*"
        Resource = [local.raw_arn, "${local.raw_arn}/*"]
      },
    ]
  })
}

module "bench_bronze_to_silver" {
  source = "../../../modules/glue_spark_job"

  name                    = "${local.name_prefix}-bench-bronze-to-silver"
  description             = "Benchmarks (ADR 0034): apply a postgres_bench snapshot batch from Bronze to the bench Silver tables."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/bronze_to_silver.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.bench_silver_policy
  log_group_arns          = [for g in aws_cloudwatch_log_group.glue : g.arn]

  job_arguments = {
    "--datalake-formats" = "iceberg"
    "--conf"             = local.bench_conf
    "--artifacts-bucket" = local.buckets["artifacts"]
    "--catalog"          = "glue_catalog"
    "--silver-database"  = local.bench_silver_db
    "--silver-location"  = "s3://${local.buckets["lake"]}/bench/silver"
    "--quarantine-root"  = "s3://${local.buckets["raw"]}/quarantine/bench"
    "--source-system"    = local.bench_source
    "--region"           = var.region
  }
}

module "partition_evolution" {
  source = "../../../modules/glue_spark_job"

  name                    = "${local.name_prefix}-partition-evolution"
  description             = "Benchmarks (ADR 0034): Iceberg ADD PARTITION FIELD and optional full rewrite on bench Silver tables."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/evolve_partitions.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.partition_evolution_policy
  log_group_arns          = [for g in aws_cloudwatch_log_group.glue : g.arn]

  job_arguments = {
    "--datalake-formats" = "iceberg"
    "--conf"             = local.bench_conf
    "--catalog"          = "glue_catalog"
    "--database"         = local.bench_silver_db
    "--fields"           = var.bench_partition_fields
    "--rewrite"          = "false"
    "--report-bucket"    = local.buckets["artifacts"]
    "--region"           = var.region
  }
}
