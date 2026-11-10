# Customer-managed KMS key with automatic rotation.
#
# The key policy is AWS's default statement (the account root gets kms:*, which delegates
# authorisation to IAM policies; each role that uses the key is granted it in IAM, see
# modules/iam), plus, optionally, AWS services that publish to SNS topics encrypted with this
# key. Services such as CloudWatch alarms and EventBridge act as service principals that IAM
# cannot authorise, so the key policy must (SNS docs, "Encryption at rest": GenerateDataKey*
# and Decrypt). They are limited to use through SNS in this region (kms:ViaService).

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
    ])
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
