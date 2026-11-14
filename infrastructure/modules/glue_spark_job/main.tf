# An AWS Glue Spark job running a thin entrypoint from jobs/glue/ with the procurelake wheel
# (ADR 0025, ADR 0048). The module owns:
#
#   - the job's IAM role: trusted by glue.amazonaws.com from this account only, with the
#     caller's data-access policy plus write access to the job's own log groups
#   - the script, wheel and dependency bundle objects in the artifacts bucket; the wheel and
#     bundle keys contain their SHA-256, so every change is a new, immutable object and the job
#     always names exactly one build
#   - the job's two log groups, KMS-encrypted. With a security configuration that encrypts
#     CloudWatch Logs, Glue writes to /aws-glue/jobs/<security configuration>-role/<job role>/
#     {error,output} instead of the shared /aws-glue/jobs/{error,output} (AWS Glue docs,
#     "Logging for AWS Glue jobs", "Log group naming with security configuration")
#   - the job: one concurrent run (one writer per target table), no automatic retries, bookmarks
#     off (the job tracks applied batches itself)
#
# Terraform only deploys the code; it contains no transformation logic.

locals {
  wheel_sha256  = filesha256(var.wheel_path)
  bundle_sha256 = filesha256(var.dependencies_bundle_path)
  # Per-job keys: several jobs ship the same files, and each S3 object must have one owner.
  wheel_key  = "${var.artifacts_prefix}/wheels/${var.name}/${local.wheel_sha256}/${basename(var.wheel_path)}"
  bundle_key = "${var.artifacts_prefix}/wheels/${var.name}/${local.bundle_sha256}/${basename(var.dependencies_bundle_path)}"
  script_key = "${var.artifacts_prefix}/scripts/${var.name}/${basename(var.script_path)}"
  temp_uri   = "s3://${var.artifacts_bucket_name}/${var.artifacts_prefix}/temp/${var.name}/"

  # The wheel, then the zip of wheels holding every pinned dependency (ADR 0048). --no-index:
  # pip resolves only from the zip, never PyPI (AWS Glue docs, "Zip of Wheels"). --no-deps: the
  # wheel's own requirements (boto3 from the Glue runtime, psycopg and requests unused by the
  # jobs) are not resolved; the zip's pins are the complete set (ADR 0025).
  additional_python_modules = join(",", [
    "s3://${var.artifacts_bucket_name}/${local.wheel_key}",
    "s3://${var.artifacts_bucket_name}/${local.bundle_key}",
  ])

  # Glue 5.x log groups under a security configuration that encrypts CloudWatch Logs.
  log_group_names = {
    for stream in ["error", "output"] :
    stream => "/aws-glue/jobs/${var.security_configuration_name}-role/${var.name}/${stream}"
  }

  default_arguments = merge(
    {
      "--job-language"                    = "python"
      "--job-bookmark-option"             = "job-bookmark-disable"
      "--TempDir"                         = local.temp_uri
      "--additional-python-modules"       = local.additional_python_modules
      "--python-modules-installer-option" = "--no-index --no-deps"
      "--wheel-sha256"                    = local.wheel_sha256
      "--dependencies-sha256"             = local.bundle_sha256
    },
    var.job_arguments,
  )
}

data "aws_partition" "current" {}
data "aws_region" "current" {}

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

resource "aws_s3_object" "dependencies" {
  bucket       = var.artifacts_bucket_name
  key          = local.bundle_key
  source       = var.dependencies_bundle_path
  source_hash  = filemd5(var.dependencies_bundle_path)
  content_type = "application/zip"
  tags         = var.tags
}

# ----------------------------------------------------------------------------- logs
# Created here, before the first run, so retention and the KMS key are managed. The key policy
# must let CloudWatch Logs use the key for these groups (modules/kms,
# cloudwatch_logs_log_group_patterns), or creating them fails.
resource "aws_cloudwatch_log_group" "job" {
  for_each = local.log_group_names

  name              = each.value
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
  tags              = var.tags
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
        # logs:AssociateKmsKey: without it Glue disables continuous logging for a job with a
        # CloudWatch-encrypting security configuration (AWS Glue docs, "Required IAM
        # Permissions"). Its resource type is the log group (Service Authorization Reference).
        Sid      = "WriteJobLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:AssociateKmsKey"]
        Resource = flatten([for g in aws_cloudwatch_log_group.job : [g.arn, "${g.arn}:*"]])
      },
      {
        # Writing to a KMS-encrypted log group (CloudWatch Logs docs, "Encrypt log data in
        # CloudWatch Logs using AWS KMS": permissions for reading and writing encrypted log data).
        Sid       = "UseLogKeyThroughCloudWatchLogs"
        Effect    = "Allow"
        Action    = ["kms:Encrypt", "kms:ReEncrypt*", "kms:Decrypt", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = var.log_kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "logs.${data.aws_region.current.region}.amazonaws.com" } }
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
          "arn:${data.aws_partition.current.partition}:s3:::${var.artifacts_bucket_name}/${local.bundle_key}",
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

  # CloudWatch Logs encryption with the platform key (ADR 0048).
  security_configuration = var.security_configuration_name

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

  depends_on = [
    aws_s3_object.wheel,
    aws_s3_object.dependencies,
    aws_cloudwatch_log_group.job,
    aws_iam_role_policy.runtime,
    aws_iam_role_policy.data_access,
  ]
}
