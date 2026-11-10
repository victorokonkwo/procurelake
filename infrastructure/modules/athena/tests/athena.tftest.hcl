mock_provider "aws" {}

variables {
  name                = "procurelake-dev-engineering"
  results_bucket_name = "procurelake-dev-artifacts-123456789012-ca-central-1"
  kms_key_arn         = "arn:aws:kms:ca-central-1:123456789012:key/test"
}

run "enforced_encrypted_engine_v3_workgroup" {
  command = apply

  assert {
    condition     = aws_athena_workgroup.this.configuration[0].enforce_workgroup_configuration
    error_message = "Workgroup configuration must be enforced."
  }
  assert {
    condition     = aws_athena_workgroup.this.configuration[0].engine_version[0].selected_engine_version == "Athena engine version 3"
    error_message = "Athena engine version 3 is required (ADR 0005)."
  }
  assert {
    condition = (
      aws_athena_workgroup.this.configuration[0].result_configuration[0].encryption_configuration[0].encryption_option == "SSE_KMS" &&
      aws_athena_workgroup.this.configuration[0].result_configuration[0].encryption_configuration[0].kms_key_arn == var.kms_key_arn
    )
    error_message = "Results must be SSE-KMS encrypted with the platform key."
  }
  assert {
    condition     = aws_athena_workgroup.this.configuration[0].result_configuration[0].output_location == "s3://procurelake-dev-artifacts-123456789012-ca-central-1/athena_results/procurelake-dev-engineering/"
    error_message = "Results must land under artifacts/athena_results/<workgroup>/ (30-day lifecycle)."
  }
  assert {
    condition     = aws_athena_workgroup.this.configuration[0].bytes_scanned_cutoff_per_query == 1073741824
    error_message = "Default per-query scan cutoff must be 1 GiB."
  }
}

run "rejects_cutoff_below_athena_minimum" {
  command = plan

  variables {
    bytes_scanned_cutoff_per_query = 1000
  }
  expect_failures = [var.bytes_scanned_cutoff_per_query]
}
