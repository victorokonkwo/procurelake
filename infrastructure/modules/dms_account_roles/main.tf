# Account-level IAM roles that AWS DMS looks up by exact name (AWS DMS User Guide, "Creating the
# IAM roles to use with AWS DMS": "You must use these exact role names ... AWS DMS looks for
# these roles by name"). They live in foundation (free, persistent) so that the per-session CDC
# stack never races their creation. dms-access-for-endpoint is only needed for Redshift.

locals {
  trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dms.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

data "aws_partition" "current" {}

resource "aws_iam_role" "vpc" {
  name               = "dms-vpc-role"
  description        = "Lets AWS DMS manage network interfaces for replication instances."
  assume_role_policy = local.trust
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.vpc.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonDMSVPCManagementRole"
}

resource "aws_iam_role" "cloudwatch_logs" {
  name               = "dms-cloudwatch-logs-role"
  description        = "Lets AWS DMS write replication task logs to CloudWatch Logs."
  assume_role_policy = local.trust
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "cloudwatch_logs" {
  role       = aws_iam_role.cloudwatch_logs.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonDMSCloudWatchLogsRole"
}
