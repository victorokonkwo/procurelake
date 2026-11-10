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

output "apply_cdc_job_name" {
  description = "Glue job that applies Bronze CDC files to Silver."
  value       = module.apply_cdc.job_name
}

output "cdc_checkpoint_table_name" {
  description = "DynamoDB table with CDC leases and checkpoints."
  value       = aws_dynamodb_table.cdc_checkpoints.name
}

output "dbt_runner_repository_url" {
  description = "ECR repository for the dbt runner image."
  value       = module.dbt_runner.repository_url
}

output "cdc_processing_state_machine_arn" {
  description = "Step Functions state machine for CDC processing (null until the dbt image exists)."
  value       = local.orchestrated ? aws_sfn_state_machine.cdc_processing[0].arn : null
}

output "dbt_transformation_state_machine_arn" {
  description = "Step Functions state machine for dbt (null until the dbt image exists)."
  value       = local.orchestrated ? aws_sfn_state_machine.dbt_transformation[0].arn : null
}

output "pipeline_alerts_topic_arn" {
  description = "SNS topic for pipeline failure alerts."
  value       = aws_sns_topic.pipeline_alerts.arn
}
