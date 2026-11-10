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

  trust_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Sid       = "AllowAccountPrincipals"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
        Action    = "sts:AssumeRole"
      }],
      length(var.trusted_service_principals) == 0 ? [] : [{
        Sid       = "AllowServices"
        Effect    = "Allow"
        Principal = { Service = var.trusted_service_principals }
        Action    = "sts:AssumeRole"
        Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
      }]
    )
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
  assume_role_policy   = local.trust_policy
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
