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
