output "bronze_to_silver_job_name" {
  description = "Glue job that applies PostgreSQL snapshots to Silver."
  value       = module.bronze_to_silver.job_name
}

output "bronze_to_silver_role_arn" {
  description = "Execution role of the Silver job."
  value       = module.bronze_to_silver.role_arn
}

output "bronze_to_silver_wheel_sha256" {
  description = "SHA-256 of the wheel the Silver job runs."
  value       = module.bronze_to_silver.wheel_sha256
}
