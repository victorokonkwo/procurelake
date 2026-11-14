# Customer-managed KMS key with automatic rotation.
#
# The key policy is AWS's default statement (the account root gets kms:*, which delegates
# authorisation to IAM policies; each role that uses the key is granted it in IAM, see
# modules/iam), plus, optionally, AWS services that publish to SNS topics encrypted with this
# key. Services such as CloudWatch alarms and EventBridge act as service principals that IAM
# cannot authorise, so the key policy must (SNS docs, "Encryption at rest": GenerateDataKey*
# and Decrypt). They are limited to use through SNS in this region (kms:ViaService).
# Optionally, CloudWatch Logs may use the key for named log groups (ADR 0048).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

resource "aws_kms_key" "this" {
  description             = var.description
  enable_key_rotation     = true
  deletion_window_in_days = var.deletion_window_in_days
  tags                    = var.tags

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "key-default-1"
    Statement = concat([
      {
        Sid       = "Enable IAM User Permissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      ], length(var.sns_publishing_services) == 0 ? [] : [
      {
        Sid       = "AwsServicesPublishToEncryptedSnsTopics"
        Effect    = "Allow"
        Principal = { Service = [for s in var.sns_publishing_services : "${s}.amazonaws.com"] }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "sns.${data.aws_region.current.region}.amazonaws.com" } }
      },
      ], length(var.cloudwatch_logs_log_group_patterns) == 0 ? [] : [
      {
        # CloudWatch Logs encrypts log groups with the key (ADR 0048). The statement is the one
        # in the CloudWatch Logs docs ("Encrypt log data in CloudWatch Logs using AWS KMS",
        # step 2), limited to the given log groups of this account through the encryption
        # context CloudWatch Logs sets (kms:EncryptionContext:aws:logs:arn).
        Sid       = "CloudWatchLogsEncryptLogGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${data.aws_region.current.region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = [
              for p in var.cloudwatch_logs_log_group_patterns :
              "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${p}"
            ]
          }
        }
      },
    ])
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
