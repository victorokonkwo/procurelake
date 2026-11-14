output "bucket_names" {
  description = "Lakehouse bucket names keyed by role."
  value       = module.lakehouse.bucket_names
}

output "data_kms_key_arn" {
  description = "KMS key encrypting lakehouse buckets."
  value       = module.data_key.key_arn
}

output "glue_security_configuration_name" {
  description = "Name of the Glue security configuration whose job log groups the data key may encrypt (ADR 0048)."
  value       = local.glue_security_configuration_name
}

output "ingestion_role_arn" {
  description = "Role for landing raw data in Bronze."
  value       = module.iam.ingestion_role_arn
}

output "data_engineer_role_arn" {
  description = "Role for building Silver/Gold."
  value       = module.iam.data_engineer_role_arn
}

output "bronze_database_name" {
  description = "Glue database for Bronze tables."
  value       = module.catalog.bronze_database_name
}

output "athena_workgroup_name" {
  description = "Athena workgroup for engineering queries."
  value       = module.athena.workgroup_name
}

output "silver_database_name" {
  description = "Glue database for Silver Iceberg tables."
  value       = module.catalog.silver_database_name
}

output "gold_database_name" {
  description = "Glue database for Gold models (dbt target schema)."
  value       = module.catalog.gold_database_name
}

output "bench_silver_database_name" {
  description = "Benchmark Silver database (Week 10, ADR 0034)."
  value       = module.catalog.bench_silver_database_name
}

output "bench_gold_database_name" {
  description = "Benchmark Gold database: dbt target for the benchmarks (ADR 0034)."
  value       = module.catalog.bench_gold_database_name
}
