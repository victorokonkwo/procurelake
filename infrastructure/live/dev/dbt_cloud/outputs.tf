output "user_name" {
  description = "IAM user dbt Cloud connects as; create its access key by hand (docs/runbooks/dbt-cloud.md)."
  value       = aws_iam_user.dbt_cloud.name
}

output "user_arn" {
  description = "ARN of the dbt Cloud IAM user."
  value       = aws_iam_user.dbt_cloud.arn
}

output "dbt_cloud_connection" {
  description = "Values for the dbt Cloud Athena connection and development credentials."
  value = {
    region_name    = var.region
    database       = "awsdatacatalog"
    work_group     = module.workgroup.workgroup_name
    s3_staging_dir = "s3://${local.artifacts_bucket}/${local.results_prefix}"
    s3_data_dir    = "s3://${local.lake_bucket}/${local.sandbox_prefix}"
    s3_data_naming = "schema_table_unique"
    schema         = aws_glue_catalog_database.sandbox.name
  }
}
