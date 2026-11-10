mock_provider "aws" {}

variables {
  description = "test key"
  alias       = "procurelake-dev-data"
}

run "rotation_enabled_with_alias" {
  command = apply

  assert {
    condition     = aws_kms_key.this.enable_key_rotation && aws_kms_key.this.deletion_window_in_days == 30
    error_message = "Key rotation must be enabled with a 30-day deletion window."
  }
  assert {
    condition     = aws_kms_alias.this.name == "alias/procurelake-dev-data"
    error_message = "Alias must be prefixed with alias/."
  }
}

run "rejects_aws_reserved_alias" {
  command = plan

  variables {
    alias = "aws/s3"
  }
  expect_failures = [var.alias]
}
