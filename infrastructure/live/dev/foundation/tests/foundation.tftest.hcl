mock_provider "aws" {}

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
