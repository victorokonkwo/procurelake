mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-bronze-to-silver" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ca-central-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:ca-central-1:123456789012:table/procurelake-dev-cdc-checkpoints" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      bucket_names = {
        raw       = "procurelake-dev-raw-123456789012-ca-central-1"
        lake      = "procurelake-dev-lake-123456789012-ca-central-1"
        products  = "procurelake-dev-products-123456789012-ca-central-1"
        artifacts = "procurelake-dev-artifacts-123456789012-ca-central-1"
      }
      data_kms_key_arn     = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
      silver_database_name = "procurelake_dev_silver"
    }
  }
}

variables {
  aws_account_id = "123456789012"
  state_bucket   = "procurelake-tfstate-123456789012-ca-central-1"
  dist_dir       = "../../../modules/glue_spark_job/tests/fixtures"
}

run "silver_job_is_wired_to_the_glue_iceberg_catalog" {
  command = apply

  assert {
    condition     = output.bronze_to_silver_job_name == "procurelake-dev-bronze-to-silver"
    error_message = "Unexpected job name."
  }
  assert {
    condition     = strcontains(module.bronze_to_silver.job_arguments_for_test["--conf"], "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog")
    error_message = "The Iceberg catalog must be backed by the Glue Data Catalog."
  }
  assert {
    condition     = module.bronze_to_silver.job_arguments_for_test["--silver-database"] == "procurelake_dev_silver"
    error_message = "The job must write the foundation's Silver database."
  }
  assert {
    condition     = strcontains(module.bronze_to_silver.job_arguments_for_test["--additional-python-modules"], ",pydantic==") && strcontains(module.bronze_to_silver.job_arguments_for_test["--additional-python-modules"], ",pyyaml==")
    error_message = "Pins from jobs/glue/silver_python_modules.txt must be passed to the job."
  }
}

run "silver_job_cannot_delete_bronze" {
  command = apply

  assert {
    condition = anytrue([
      for s in jsondecode(local.bronze_to_silver_policy).Statement :
      s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*")
    ])
    error_message = "The Silver job role must be denied deleting Bronze objects."
  }
  assert {
    condition = alltrue([
      for s in jsondecode(local.bronze_to_silver_policy).Statement :
      s.Effect == "Deny" || !contains(flatten([s.Resource]), "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/*")
    ])
    error_message = "No allow statement may cover the whole raw bucket."
  }
  assert {
    condition = alltrue(flatten([
      for s in jsondecode(local.bronze_to_silver_policy).Statement : [
        for r in flatten([s.Resource]) : !endswith(r, ":*") && r != "*"
      ]
    ]))
    error_message = "No wildcard-only resources."
  }
}

run "cdc_job_uses_the_checkpoint_table_and_cannot_delete_bronze" {
  command = apply

  assert {
    condition     = module.apply_cdc.job_arguments_for_test["--checkpoint-table"] == "procurelake-dev-cdc-checkpoints" && output.apply_cdc_job_name == "procurelake-dev-apply-cdc"
    error_message = "The CDC job must use the checkpoint table."
  }
  assert {
    condition     = aws_dynamodb_table.cdc_checkpoints.deletion_protection_enabled && aws_dynamodb_table.cdc_checkpoints.server_side_encryption[0].enabled && aws_dynamodb_table.cdc_checkpoints.point_in_time_recovery[0].enabled
    error_message = "Checkpoints must be protected from deletion, encrypted and recoverable."
  }
  assert {
    condition = anytrue([for s in jsondecode(local.apply_cdc_policy).Statement :
    s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*")])
    error_message = "The CDC job role must be denied Bronze deletes."
  }
  assert {
    condition = alltrue([for s in jsondecode(local.apply_cdc_policy).Statement :
    s.Effect == "Deny" || !contains(flatten([s.Resource]), "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/postgres_cdc/*") || flatten([s.Action]) == ["s3:GetObject"]])
    error_message = "The CDC job may only read DMS output in Bronze."
  }
  assert {
    condition     = module.apply_cdc.job_arguments_for_test["--conf"] == module.bronze_to_silver.job_arguments_for_test["--conf"]
    error_message = "Both Silver writers must use the same Iceberg catalog configuration."
  }
}
