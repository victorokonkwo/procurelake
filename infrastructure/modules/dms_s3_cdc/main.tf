# AWS DMS full load + CDC from PostgreSQL into Bronze as Parquet (ADR 0027).
#
# Settings follow the AWS DMS User Guide (PostgreSQL source; S3 target):
#   - Parquet, with TimestampColumnName: for CDC rows the value is "the timestamp for the commit
#     of that row in the source database"; for full-load rows, with
#     UseTaskStartTimeForFullLoadTimestamp, the task start time (sortable with CDC rows).
#   - IncludeOpForFullLoad, so every row carries its operation (I/U/D).
#   - Parquet output does not preserve transaction order across tables. Consumers order per
#     table by commit timestamp (ADR 0014, ADR 0027).
#   - CaptureDdls=false: no event triggers or DDL artifacts on the source; schema changes are
#     caught by the source contracts instead.
#   - TargetTablePrepMode DO_NOTHING: DMS never deletes target files.
#
# Bronze immutability: the DMS S3 role is denied deletes under bronze/, against the docs'
# listed s3:DeleteObject requirement (owner decision, ADR 0027). Each stack instance writes under
# its own load_id= prefix, so a new session's full load (LOAD00000001.parquet ...) can never
# overwrite an earlier session's files.

data "aws_partition" "current" {}

locals {
  p          = data.aws_partition.current.partition
  raw_arn    = "arn:${local.p}:s3:::${var.raw_bucket_name}"
  cdc_prefix = "${var.bronze_prefix}/load_id=${terraform_data.load_id.output}"
}

# Fixed when the stack is created; ignore_changes keeps it stable on later plans.
resource "terraform_data" "load_id" {
  input = formatdate("YYYYMMDD'T'hhmmss'Z'", timestamp())

  lifecycle {
    ignore_changes = [input]
  }
}

# ----------------------------------------------------------------------------- IAM
resource "aws_iam_role" "s3_target" {
  name        = "${var.name}-s3-target"
  description = "AWS DMS writes CDC Parquet into Bronze (no deletes)."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dms.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "s3_target" {
  name = "bronze-cdc-write"
  role = aws_iam_role.s3_target.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # DMS calls HeadBucket first, which S3 authorizes as s3:ListBucket with no s3:prefix, so a
        # prefix condition denies it ("Failed to head bucket", first dev run). The DMS User Guide
        # lists s3:ListBucket on the bucket without a condition. Listing exposes key names only;
        # the role still cannot read or delete objects.
        Sid      = "ListAndHeadBucket"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = local.raw_arn
      },
      {
        Sid      = "WriteCdcFiles"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectTagging"]
        Resource = "${local.raw_arn}/${var.bronze_prefix}/*"
      },
      {
        Sid      = "EncryptWithDataKey"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
      {
        Sid       = "DataKeyGrantsForAwsResources"
        Effect    = "Allow"
        Action    = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
        Resource  = var.kms_key_arn
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      },
      {
        Sid      = "DenyBronzeDeletes"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = "${local.raw_arn}/bronze/*"
      },
    ]
  })
}

resource "aws_iam_role" "secrets" {
  name        = "${var.name}-secrets"
  description = "AWS DMS reads the replication user's secret."
  # The DMS User Guide specifies the regional service principal for secret access roles.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dms.${var.region}.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "secrets" {
  name = "read-replication-secret"
  role = aws_iam_role.secrets.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadReplicationSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = var.replication_secret_arn
      },
      {
        Sid      = "DecryptReplicationSecret"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

# ----------------------------------------------------------------------------- replication
resource "aws_dms_replication_subnet_group" "this" {
  replication_subnet_group_id          = var.name
  replication_subnet_group_description = "Private subnets for ${var.name} (no internet route)."
  subnet_ids                           = var.subnet_ids
  tags                                 = var.tags
}

resource "aws_dms_replication_instance" "this" {
  replication_instance_id     = var.name
  replication_instance_class  = var.instance_class
  engine_version              = var.engine_version
  allocated_storage           = 20
  multi_az                    = false
  publicly_accessible         = false
  replication_subnet_group_id = aws_dms_replication_subnet_group.this.id
  vpc_security_group_ids      = [var.security_group_id]
  kms_key_arn                 = var.kms_key_arn
  auto_minor_version_upgrade  = false
  apply_immediately           = true
  tags                        = var.tags
}

resource "aws_dms_endpoint" "source" {
  endpoint_id                     = "${var.name}-source"
  endpoint_type                   = "source"
  engine_name                     = "postgres"
  database_name                   = var.database_name
  secrets_manager_arn             = var.replication_secret_arn
  secrets_manager_access_role_arn = aws_iam_role.secrets.arn
  ssl_mode                        = "require"

  postgres_settings {
    capture_ddls        = false
    heartbeat_enable    = true
    heartbeat_frequency = 5
    # Created by `make cdc-source-setup`; the DMS user cannot create objects in public.
    heartbeat_schema = var.artifacts_schema
    plugin_name      = "test-decoding"
  }

  tags       = var.tags
  depends_on = [aws_iam_role_policy.secrets]
}

resource "aws_dms_s3_endpoint" "target" {
  endpoint_id             = "${var.name}-bronze"
  endpoint_type           = "target"
  bucket_name             = var.raw_bucket_name
  bucket_folder           = local.cdc_prefix
  service_access_role_arn = aws_iam_role.s3_target.arn

  data_format                                 = "parquet"
  parquet_version                             = "parquet-2-0"
  enable_statistics                           = true
  timestamp_column_name                       = var.commit_timestamp_column
  use_task_start_time_for_full_load_timestamp = true
  include_op_for_full_load                    = true
  cdc_max_batch_interval                      = var.cdc_max_batch_interval_seconds
  encryption_mode                             = "SSE_KMS"
  server_side_encryption_kms_key_id           = var.kms_key_arn

  tags       = var.tags
  depends_on = [aws_iam_role_policy.s3_target]
}

resource "aws_dms_replication_task" "this" {
  replication_task_id      = "${var.name}-full-load-and-cdc"
  migration_type           = "full-load-and-cdc"
  replication_instance_arn = aws_dms_replication_instance.this.replication_instance_arn
  source_endpoint_arn      = aws_dms_endpoint.source.endpoint_arn
  target_endpoint_arn      = aws_dms_s3_endpoint.target.endpoint_arn
  start_replication_task   = var.start_task

  table_mappings = jsonencode({
    rules = [{
      "rule-type"      = "selection"
      "rule-id"        = "1"
      "rule-name"      = "procurement-schema"
      "object-locator" = { "schema-name" = var.source_schema, "table-name" = "%" }
      "rule-action"    = "include"
    }]
  })

  replication_task_settings = jsonencode({
    FullLoadSettings = { TargetTablePrepMode = "DO_NOTHING" }
    Logging          = { EnableLogging = true }
  })

  tags = var.tags

  lifecycle {
    # DMS fills in many task-setting defaults after creation.
    ignore_changes = [replication_task_settings]
  }
}
