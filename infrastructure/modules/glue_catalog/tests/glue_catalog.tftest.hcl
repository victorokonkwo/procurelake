mock_provider "aws" {}

variables {
  name_prefix           = "procurelake_dev"
  environment           = "dev"
  raw_bucket_name       = "procurelake-dev-raw-123456789012-ca-central-1"
  artifacts_bucket_name = "procurelake-dev-artifacts-123456789012-ca-central-1"
}

run "bronze_database_and_raw_table" {
  command = apply

  assert {
    condition     = aws_glue_catalog_database.bronze.name == "procurelake_dev_bronze"
    error_message = "Unexpected Bronze database name."
  }
  assert {
    condition     = aws_glue_catalog_table.usaspending_spending_by_award_raw.storage_descriptor[0].columns[0].name == "raw_json" && length(aws_glue_catalog_table.usaspending_spending_by_award_raw.storage_descriptor[0].columns) == 1
    error_message = "Raw table must expose exactly one raw_json column."
  }
  assert {
    condition     = aws_glue_catalog_table.usaspending_spending_by_award_raw.storage_descriptor[0].location == "s3://procurelake-dev-raw-123456789012-ca-central-1/bronze/usaspending/spending_by_award/"
    error_message = "Raw table must point at the USAspending Bronze prefix."
  }
}

run "partition_projection_matches_landing_layout" {
  command = apply

  assert {
    condition     = aws_glue_catalog_table.usaspending_spending_by_award_raw.parameters["projection.enabled"] == "true"
    error_message = "Partition projection must be enabled."
  }
  assert {
    condition     = aws_glue_catalog_table.usaspending_spending_by_award_raw.parameters["storage.location.template"] == "s3://procurelake-dev-raw-123456789012-ca-central-1/bronze/usaspending/spending_by_award/ingestion_date=$${ingestion_date}/"
    error_message = "Location template must match ingestion's bronze/<source>/<dataset>/ingestion_date=<date>/ layout."
  }
  assert {
    condition     = aws_glue_catalog_table.usaspending_spending_by_award_raw.partition_keys[0].name == "ingestion_date"
    error_message = "Partition key must be ingestion_date."
  }
}

run "rejects_unsafe_database_prefix" {
  command = plan

  variables {
    name_prefix = "procurelake-dev"
  }
  expect_failures = [var.name_prefix]
}
