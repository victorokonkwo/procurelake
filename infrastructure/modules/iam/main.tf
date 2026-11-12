# Workload identities for the dev environment (ADR 0011: one account, identities separated
# by IAM role). Consumer/tenant roles are added in Week 7; Glue and Athena permissions are
# added when those services are introduced.
#
#   ingestion      - lands raw data in Bronze and writes ingestion manifests
#   data_engineer  - reads Bronze; reads/writes Silver, Gold, products, and artifacts
#
# Both roles are explicitly denied deleting Bronze objects or versions (ADR 0002).

locals {
  raw_arn       = var.bucket_arns["raw"]
  lake_arn      = var.bucket_arns["lake"]
  products_arn  = var.bucket_arns["products"]
  artifacts_arn = var.bucket_arns["artifacts"]

  account_trust = {
    Sid       = "AllowAccountPrincipals"
    Effect    = "Allow"
    Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
    Action    = "sts:AssumeRole"
  }
  service_trust = [for services in [var.trusted_service_principals] : {
    Sid       = "AllowServices"
    Effect    = "Allow"
    Principal = { Service = services }
    Action    = "sts:AssumeRole"
    Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
  } if length(services) > 0]

  trust_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat([local.account_trust], local.service_trust)
  })

  # The data engineer may additionally be assumed by services that run dbt for it (ECS tasks,
  # ADR 0029); the ingestion role never is.
  data_engineer_services = distinct(concat(var.trusted_service_principals, var.data_engineer_service_principals))
  data_engineer_trust_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([local.account_trust], length(local.data_engineer_services) == 0 ? [] : [{
      Sid       = "AllowServices"
      Effect    = "Allow"
      Principal = { Service = local.data_engineer_services }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }])
  })

  kms_statement = {
    Sid      = "UsePlatformKey"
    Effect   = "Allow"
    Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    Resource = var.kms_key_arn
  }

  deny_bronze_delete_statement = {
    Sid      = "DenyBronzeDeletes"
    Effect   = "Deny"
    Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    Resource = "${local.raw_arn}/*"
  }

  ingestion_raw_prefixes = ["bronze/", "documents/", "quarantine/"]
}

# ----------------------------------------------------------------------------- ingestion
resource "aws_iam_role" "ingestion" {
  name                 = "${var.name_prefix}-ingestion"
  description          = "Lands raw source data in Bronze and writes ingestion manifests."
  assume_role_policy   = local.trust_policy
  max_session_duration = 3600
  tags                 = var.tags
}

resource "aws_iam_role_policy" "ingestion" {
  name = "ingestion-access"
  role = aws_iam_role.ingestion.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListRawLandingPrefixes"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = local.raw_arn
        Condition = {
          StringLike = { "s3:prefix" = [for p in local.ingestion_raw_prefixes : "${p}*"] }
        }
      },
      {
        Sid      = "ReadWriteRawLanding"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [for p in local.ingestion_raw_prefixes : "${local.raw_arn}/${p}*"]
      },
      {
        # Large CSVs land as multipart uploads (ADR 0039). Aborting a failed upload discards
        # its parts; it cannot delete or overwrite a completed object.
        Sid      = "AbortOwnFailedUploads"
        Effect   = "Allow"
        Action   = "s3:AbortMultipartUpload"
        Resource = "${local.raw_arn}/bronze/*"
      },
      {
        Sid       = "ListManifests"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.artifacts_arn
        Condition = { StringLike = { "s3:prefix" = ["pipeline_manifests/*"] } }
      },
      {
        Sid      = "ReadWriteManifests"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.artifacts_arn}/pipeline_manifests/*"
      },
      local.kms_statement,
      local.deny_bronze_delete_statement,
    ]
  })
}

# ----------------------------------------------------------------------------- data engineer
resource "aws_iam_role" "data_engineer" {
  name                 = "${var.name_prefix}-data-engineer"
  description          = "Builds Silver/Gold: reads Bronze, writes lake, products and artifacts."
  assume_role_policy   = local.data_engineer_trust_policy
  max_session_duration = 3600
  tags                 = var.tags
}

resource "aws_iam_role_policy" "data_engineer" {
  name = "data-engineer-access"
  role = aws_iam_role.data_engineer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListPlatformBuckets"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = [local.raw_arn, local.lake_arn, local.products_arn, local.artifacts_arn]
      },
      {
        Sid      = "ReadBronze"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${local.raw_arn}/*"
      },
      {
        Sid    = "ReadWriteLakeProductsArtifacts"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = [
          "${local.lake_arn}/*",
          "${local.products_arn}/*",
          "${local.artifacts_arn}/*",
        ]
      },
      local.kms_statement,
      local.deny_bronze_delete_statement,
    ]
  })
}

# ----------------------------------------------------------------------------- query access
# Athena + Glue Data Catalog read access for the data engineer only (ingestion never queries).
locals {
  glue_catalog_arn = "arn:aws:glue:${var.region}:${var.aws_account_id}:catalog"
  glue_resource_arns = concat(
    [local.glue_catalog_arn],
    [for db in var.glue_database_names : "arn:aws:glue:${var.region}:${var.aws_account_id}:database/${db}"],
    [for db in var.glue_database_names : "arn:aws:glue:${var.region}:${var.aws_account_id}:table/${db}/*"],
  )
}

resource "aws_iam_role_policy" "data_engineer_query" {
  count = length(var.athena_workgroup_arns) > 0 ? 1 : 0

  name = "data-engineer-query"
  role = aws_iam_role.data_engineer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunAthenaQueriesInPlatformWorkgroups"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:StopQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:GetQueryRuntimeStatistics",
          "athena:GetWorkGroup",
          "athena:ListQueryExecutions",
          "athena:GetNamedQuery",
          "athena:ListNamedQueries",
          "athena:BatchGetNamedQuery",
        ]
        Resource = var.athena_workgroup_arns
      },
      {
        Sid    = "ReadPlatformCatalog"
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
        Resource = local.glue_resource_arns
      },
      {
        # Athena writes results with multipart uploads under artifacts/athena_results/.
        Sid      = "AthenaResultMultipart"
        Effect   = "Allow"
        Action   = ["s3:ListBucketMultipartUploads", "s3:ListMultipartUploadParts"]
        Resource = [local.artifacts_arn, "${local.artifacts_arn}/athena_results/*"]
      },
    ]
  })
}

# dbt-athena builds Gold with Athena CTAS/views and swaps Iceberg tables through the Glue API.
# The Glue calls below are the ones the dbt-athena 1.11.1 adapter makes (read from its source,
# ADR 0026), restricted to the Gold databases. No CreateDatabase: Terraform owns the databases.
locals {
  glue_writable_arns = concat(
    [for db in var.glue_writable_database_names : "arn:aws:glue:${var.region}:${var.aws_account_id}:database/${db}"],
    [for db in var.glue_writable_database_names : "arn:aws:glue:${var.region}:${var.aws_account_id}:table/${db}/*"],
  )
}

resource "aws_iam_role_policy" "data_engineer_build" {
  count = length(var.glue_writable_database_names) > 0 ? 1 : 0

  name = "data-engineer-build"
  role = aws_iam_role.data_engineer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageGoldTables"
        Effect = "Allow"
        Action = [
          "glue:CreateTable",
          "glue:UpdateTable",
          "glue:DeleteTable",
          "glue:GetTableVersions",
          "glue:DeleteTableVersion",
          "glue:BatchDeleteTableVersion",
        ]
        Resource = concat(["arn:aws:glue:${var.region}:${var.aws_account_id}:catalog"], local.glue_writable_arns)
      },
    ]
  })
}
