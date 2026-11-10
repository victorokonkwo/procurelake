output "ingestion_role_arn" {
  description = "ARN of the ingestion role."
  value       = aws_iam_role.ingestion.arn
}

output "data_engineer_role_arn" {
  description = "ARN of the data engineer role."
  value       = aws_iam_role.data_engineer.arn
}
