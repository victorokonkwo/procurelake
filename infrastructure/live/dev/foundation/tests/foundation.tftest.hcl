mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  # Athena validates ARN syntax, so the mock KMS key needs a well-formed ARN.
  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  }
}

variables {
  aws_account_id = "123456789012"
}

run "wires_key_buckets_and_roles" {
  command = apply

  assert {
    condition = output.bucket_names == {
      raw       = "procurelake-dev-raw-123456789012-ca-central-1"
      lake      = "procurelake-dev-lake-123456789012-ca-central-1"
      products  = "procurelake-dev-products-123456789012-ca-central-1"
      artifacts = "procurelake-dev-artifacts-123456789012-ca-central-1"
    }
    error_message = "dev foundation must create the four documented buckets."
  }
  assert {
    condition     = output.ingestion_role_arn != "" && output.data_engineer_role_arn != ""
    error_message = "Both workload roles must be created."
  }
}

run "wires_catalog_and_workgroup" {
  command = apply

  assert {
    condition     = output.bronze_database_name == "procurelake_dev_bronze"
    error_message = "Bronze database must be named procurelake_dev_bronze."
  }
  assert {
    condition     = output.silver_database_name == "procurelake_dev_silver"
    error_message = "Silver database must be named procurelake_dev_silver."
  }
  assert {
    condition     = output.gold_database_name == "procurelake_dev_gold"
    error_message = "Gold database must be named procurelake_dev_gold."
  }
  assert {
    condition     = output.athena_workgroup_name == "procurelake-dev-engineering"
    error_message = "Engineering workgroup must be named procurelake-dev-engineering."
  }
}
