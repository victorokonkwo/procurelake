# An AWS Glue Spark job running a thin entrypoint from jobs/glue/ with the procurelake wheel
# (ADR 0025). The module owns:
#
#   - the job's IAM role: trusted by glue.amazonaws.com from this account only, with the
#     caller's data-access policy plus write access to the Glue log groups (created by the
#     caller once per account, since every Glue job shares them)
#   - the script and wheel objects in the artifacts bucket; the wheel key contains its SHA-256,
#     so every code change is a new, immutable object and the job always names exactly one build
#   - the job: one concurrent run (one writer per target table), no automatic retries, bookmarks
#     off (the job tracks applied batches itself)
#
# Terraform only deploys the code; it contains no transformation logic.

locals {
  wheel_sha256 = filesha256(var.wheel_path)
  # Per-job key: several jobs ship the same wheel, and each S3 object must have one owner.
  wheel_key  = "${var.artifacts_prefix}/wheels/${var.name}/${local.wheel_sha256}/${basename(var.wheel_path)}"
  script_key = "${var.artifacts_prefix}/scripts/${var.name}/${basename(var.script_path)}"
  temp_uri   = "s3://${var.artifacts_bucket_name}/${var.artifacts_prefix}/temp/${var.name}/"

  additional_python_modules = join(",", concat(
    ["s3://${var.artifacts_bucket_name}/${local.wheel_key}"],
    var.python_modules,
  ))

  default_arguments = merge(
    {
      "--job-language"                    = "python"
      "--job-bookmark-option"             = "job-bookmark-disable"
      "--TempDir"                         = local.temp_uri
      "--additional-python-modules"       = local.additional_python_modules
      "--python-modules-installer-option" = "--no-deps"
      "--wheel-sha256"                    = local.wheel_sha256
    },
    var.job_arguments,
  )
}

data "aws_partition" "current" {}

# ----------------------------------------------------------------------------- code artifacts
resource "aws_s3_object" "script" {
  bucket       = var.artifacts_bucket_name
  key          = local.script_key
  source       = var.script_path
  source_hash  = filemd5(var.script_path)
  content_type = "text/x-python"
  tags         = var.tags
}

resource "aws_s3_object" "wheel" {
  bucket       = var.artifacts_bucket_name
  key          = local.wheel_key
  source       = var.wheel_path
  source_hash  = filemd5(var.wheel_path)
  content_type = "application/zip"
  tags         = var.tags
}

# ----------------------------------------------------------------------------- role
resource "aws_iam_role" "job" {
  name        = var.name
  description = "Execution role for the ${var.name} Glue job."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GlueFromThisAccount"
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
  max_session_duration = 3600
  tags                 = var.tags
}

resource "aws_iam_role_policy" "data_access" {
  name   = "data-access"
  role   = aws_iam_role.job.id
  policy = var.data_access_policy_json
}

resource "aws_iam_role_policy" "runtime" {
  name = "glue-runtime"
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteJobLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = flatten([for arn in var.log_group_arns : [arn, "${arn}:*"]])
      },
      {
        Sid      = "ListOwnArtifacts"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${var.artifacts_bucket_name}"
        Condition = {
          StringLike = { "s3:prefix" = ["${var.artifacts_prefix}/*"] }
        }
      },
      {
        Sid    = "ReadOwnCode"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "arn:${data.aws_partition.current.partition}:s3:::${var.artifacts_bucket_name}/${local.script_key}",
          "arn:${data.aws_partition.current.partition}:s3:::${var.artifacts_bucket_name}/${local.wheel_key}",
        ]
      },
      {
        Sid      = "UseOwnTempDir"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${var.artifacts_bucket_name}/${var.artifacts_prefix}/temp/${var.name}/*"
      },
      {
        Sid      = "UseArtifactsKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

# ----------------------------------------------------------------------------- job
resource "aws_glue_job" "this" {
  name              = var.name
  description       = var.description
  role_arn          = aws_iam_role.job.arn
  glue_version      = var.glue_version
  worker_type       = var.worker_type
  number_of_workers = var.number_of_workers
  timeout           = var.timeout_minutes
  max_retries       = 0
  execution_class   = "STANDARD"

  command {
    name            = "glueetl"
    script_location = "s3://${var.artifacts_bucket_name}/${aws_s3_object.script.key}"
    python_version  = "3"
  }

  execution_property {
    max_concurrent_runs = 1
  }

  default_arguments = local.default_arguments

  tags = var.tags

  depends_on = [aws_s3_object.wheel, aws_iam_role_policy.runtime, aws_iam_role_policy.data_access]
}
