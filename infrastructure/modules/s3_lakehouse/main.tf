# ProcureLake storage buckets (layout: docs/architecture/overview.md section 4).
#
#   raw       - Bronze landing (immutable source files, CDC events), documents, quarantine
#   lake      - Silver and Gold Apache Iceberg tables (layout controlled by Iceberg metadata)
#   products  - tenant-scoped exports and published data products
#   artifacts - pipeline manifests, dbt artifacts, Athena query results, audit records
#
# Every bucket is versioned, SSE-KMS encrypted with the platform key, blocked from public
# access, owner-enforced (ACLs disabled), and rejects non-TLS requests. Prefixes are not
# pre-created as empty objects; they come into existence when data is written.

locals {
  buckets = {
    raw = {
      purpose = "bronze-raw-landing"
      # Current versions are never expired: Bronze must stay replayable.
      noncurrent_version_expiration_days = var.raw_noncurrent_version_expiration_days
      prefix_expirations                 = {}
    }
    lake = {
      purpose = "silver-gold-iceberg"
      # Iceberg compaction/VACUUM rewrite and delete files; keep superseded versions briefly.
      noncurrent_version_expiration_days = 30
      prefix_expirations                 = {}
    }
    products = {
      purpose                            = "tenant-data-products"
      noncurrent_version_expiration_days = 30
      prefix_expirations                 = {}
    }
    artifacts = {
      purpose                            = "pipeline-artifacts"
      noncurrent_version_expiration_days = 30
      prefix_expirations = {
        "athena_results/" = var.athena_results_expiration_days
      }
    }
  }
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket        = "${var.name_prefix}-${each.key}-${var.aws_account_id}-${var.region}"
  force_destroy = false

  tags = merge(var.tags, { Purpose = each.value.purpose })
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = aws_s3_bucket.this

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "this" {
  for_each = aws_s3_bucket.this

  bucket = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [each.value.arn, "${each.value.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = each.value.noncurrent_version_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  dynamic "rule" {
    for_each = each.value.prefix_expirations
    content {
      id     = "expire-${trimsuffix(replace(rule.key, "/", "-"), "-")}"
      status = "Enabled"
      filter {
        prefix = rule.key
      }
      expiration {
        days = rule.value
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}
