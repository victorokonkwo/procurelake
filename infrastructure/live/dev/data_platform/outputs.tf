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

output "ai_extraction_role_arn" {
  description = "Role for contract extraction and review (make ai-*)."
  value       = aws_iam_role.ai_extraction.arn
}

output "ai_review_table_name" {
  description = "DynamoDB review queue for contract extraction."
  value       = aws_dynamodb_table.ai_review.name
}

output "iceberg_maintenance_job_name" {
  description = "Daily Iceberg maintenance Glue job (make iceberg-maintenance-run)."
  value       = module.iceberg_maintenance.job_name
}

output "reference_to_silver_job_name" {
  description = "Glue job applying USAspending batches to Silver (make reference-silver-run; ADR 0044)."
  value       = module.reference_to_silver.job_name
}

output "bench_silver_job_name" {
  description = "On-demand Glue job applying postgres_bench snapshots to the bench Silver (ADR 0034)."
  value       = module.bench_bronze_to_silver.job_name
}

output "partition_evolution_job_name" {
  description = "On-demand Glue job evolving bench Silver partition specs (ADR 0034)."
  value       = module.partition_evolution.job_name
}

output "task_runner_repository_url" {
  description = "ECR repository of the task runner image (make task-runner-image)."
  value       = module.task_runner.repository_url
}

output "batch_ingestion_state_machine_arn" {
  description = "Batch ingestion state machine (ADR 0040); null until the task runner image is deployed."
  value       = local.runner_ready ? aws_sfn_state_machine.batch_ingestion[0].arn : null
}

output "data_quality_state_machine_arn" {
  description = "Daily data-quality state machine (ADR 0040); null until the task runner image is deployed."
  value       = local.runner_ready ? aws_sfn_state_machine.data_quality[0].arn : null
}
