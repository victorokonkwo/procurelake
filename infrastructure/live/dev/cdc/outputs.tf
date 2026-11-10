output "source_address" {
  description = "Source database host."
  value       = module.source.address
}

output "source_port" {
  description = "Source database port."
  value       = module.source.port
}

output "source_database_name" {
  description = "Source database name."
  value       = module.source.database_name
}

output "master_user_secret_arn" {
  description = "RDS-managed master credentials secret."
  value       = module.source.master_user_secret_arn
}

output "replication_secret_arn" {
  description = "DMS user's credentials secret."
  value       = module.source.replication_secret_arn
}

output "replication_task_arn" {
  description = "Full-load-and-CDC task (null until enable_dms = true)."
  value       = var.enable_dms ? module.dms[0].replication_task_arn : null
}

output "bronze_cdc_prefix" {
  description = "Bronze prefix of this stack instance's DMS output (null until enable_dms = true)."
  value       = var.enable_dms ? module.dms[0].bronze_cdc_prefix : null
}

output "cdc_schedule_name" {
  description = "EventBridge Scheduler schedule starting cdc_processing (null when not scheduled)."
  value       = local.with_schedule ? aws_scheduler_schedule.cdc_processing[0].name : null
}
