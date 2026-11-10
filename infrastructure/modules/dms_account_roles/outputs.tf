output "vpc_role_arn" {
  description = "ARN of dms-vpc-role."
  value       = aws_iam_role.vpc.arn
}

output "cloudwatch_logs_role_arn" {
  description = "ARN of dms-cloudwatch-logs-role."
  value       = aws_iam_role.cloudwatch_logs.arn
}
