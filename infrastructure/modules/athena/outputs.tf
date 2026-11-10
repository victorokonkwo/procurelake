output "workgroup_name" {
  description = "Athena workgroup name."
  value       = aws_athena_workgroup.this.name
}

output "workgroup_arn" {
  description = "Athena workgroup ARN."
  value       = aws_athena_workgroup.this.arn
}
