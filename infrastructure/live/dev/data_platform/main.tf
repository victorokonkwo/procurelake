# dev / data_platform: Glue jobs that build Silver (ADR 0025). Storage, keys, catalog databases
# and workload roles come from dev/foundation, read from its remote state.

locals {
  name_prefix = "procurelake-dev"

  foundation  = data.terraform_remote_state.foundation.outputs
  buckets     = local.foundation.bucket_names
  raw_arn     = "arn:aws:s3:::${local.buckets["raw"]}"
  lake_arn    = "arn:aws:s3:::${local.buckets["lake"]}"
  art_arn     = "arn:aws:s3:::${local.buckets["artifacts"]}"
  silver_db   = local.foundation.silver_database_name
  glue_prefix = "arn:aws:glue:${var.region}:${var.aws_account_id}"

  wheel_path = "${var.dist_dir}/${one(fileset(var.dist_dir, "procurelake-*-py3-none-any.whl"))}"
  python_modules = [
    for line in split("\n", file("${path.module}/../../../../jobs/glue/silver_python_modules.txt")) :
    trimspace(line) if trimspace(line) != "" && !startswith(trimspace(line), "#")
  ]
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/foundation/terraform.tfstate"
    region = var.region
  }
}

# ----------------------------------------------------------------------------- bronze -> silver
# Data access for the Silver job: read PostgreSQL snapshots in Bronze and their manifests,
# write Silver Iceberg tables, Silver quarantine and run summaries. Bronze deletes are denied.
locals {
  bronze_to_silver_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListJobPrefixesRaw"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.raw_arn
        Condition = { StringLike = { "s3:prefix" = ["bronze/postgres/*", "quarantine/silver/*"] } }
      },
      {
        Sid       = "ListJobPrefixesLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["silver/*"] } }
      },
      {
        Sid       = "ListJobPrefixesArtifacts"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["pipeline_manifests/postgres/*", "pipeline_runs/silver/*"] } }
      },
      {
        Sid      = "ReadPostgresSnapshots"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${local.raw_arn}/bronze/postgres/*", "${local.art_arn}/pipeline_manifests/postgres/*"]
      },
      {
        Sid      = "WriteQuarantineAndSilver"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.raw_arn}/quarantine/silver/*", "${local.lake_arn}/silver/*"]
      },
      {
        Sid      = "WriteRunSummaries"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.art_arn}/pipeline_runs/silver/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "ManageSilverIcebergTables"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.silver_db}",
          "${local.glue_prefix}:table/${local.silver_db}/*",
        ]
      },
      {
        # Bronze is immutable (ADR 0002). Silver quarantine is the only raw prefix it may delete
        # in, because Spark's file committer renames (copy + delete) its output there.
        Sid      = "DenyBronzeDeletes"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = ["${local.raw_arn}/bronze/*", "${local.raw_arn}/documents/*"]
      },
    ]
  })
}

module "bronze_to_silver" {
  source = "../../../modules/glue_spark_job"

  name                    = "${local.name_prefix}-bronze-to-silver"
  description             = "Apply the latest complete PostgreSQL snapshot batch from Bronze to Silver Iceberg tables."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/bronze_to_silver.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.bronze_to_silver_policy

  job_arguments = {
    # Iceberg on Glue 5.1 (AWS Glue docs, "Using the Iceberg framework in AWS Glue"): the
    # bundled Iceberg 1.10.0 runtime, with a Spark catalog backed by the Glue Data Catalog.
    "--datalake-formats" = "iceberg"
    "--conf" = join(" --conf ", [
      "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
      "spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog",
      "spark.sql.catalog.glue_catalog.warehouse=s3://${local.buckets["lake"]}/silver/",
      "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog",
      "spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
      "spark.sql.session.timeZone=UTC",
    ])
    "--artifacts-bucket" = local.buckets["artifacts"]
    "--catalog"          = "glue_catalog"
    "--silver-database"  = local.silver_db
    "--silver-location"  = "s3://${local.buckets["lake"]}/silver"
    "--quarantine-root"  = "s3://${local.buckets["raw"]}/quarantine"
    "--region"           = var.region
  }
}
