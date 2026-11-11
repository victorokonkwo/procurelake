output "bronze_database_name" {
  description = "Glue database for Bronze tables."
  value       = aws_glue_catalog_database.bronze.name
}

output "usaspending_raw_table_name" {
  description = "Raw USAspending table name."
  value       = aws_glue_catalog_table.usaspending_spending_by_award_raw.name
}

output "silver_database_name" {
  description = "Glue database for Silver Iceberg tables."
  value       = aws_glue_catalog_database.silver.name
}

output "gold_database_name" {
  description = "Glue database for Gold models."
  value       = aws_glue_catalog_database.gold.name
}

output "gold_staging_database_name" {
  description = "Glue database for dbt staging and intermediate views."
  value       = aws_glue_catalog_database.gold_staging.name
}

output "bench_silver_database_name" {
  description = "Benchmark Silver database (null unless bench_databases)."
  value       = one(aws_glue_catalog_database.bench_silver[*].name)
}

output "bench_gold_database_name" {
  description = "Benchmark Gold database (null unless bench_databases)."
  value       = one(aws_glue_catalog_database.bench_gold[*].name)
}
