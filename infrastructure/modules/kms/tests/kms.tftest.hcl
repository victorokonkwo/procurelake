mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "ca-central-1" }
  }
}

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

run "default_policy_keeps_root_and_adds_nothing" {
  command = apply

  assert {
    condition     = length(jsondecode(aws_kms_key.this.policy).Statement) == 1 && jsondecode(aws_kms_key.this.policy).Statement[0].Principal.AWS == "arn:aws:iam::123456789012:root"
    error_message = "Without services the policy is exactly the AWS default (root, delegating to IAM)."
  }
}

run "sns_publishers_only_through_sns" {
  command = apply

  variables {
    sns_publishing_services = ["cloudwatch", "events"]
  }

  assert {
    condition     = jsondecode(aws_kms_key.this.policy).Statement[0].Action == "kms:*" && jsondecode(aws_kms_key.this.policy).Statement[1].Condition.StringEquals["kms:ViaService"] == "sns.ca-central-1.amazonaws.com"
    error_message = "The root statement must stay; services may use the key only through SNS."
  }
  assert {
    condition     = toset(jsondecode(aws_kms_key.this.policy).Statement[1].Principal.Service) == toset(["cloudwatch.amazonaws.com", "events.amazonaws.com"]) && toset(jsondecode(aws_kms_key.this.policy).Statement[1].Action) == toset(["kms:GenerateDataKey*", "kms:Decrypt"])
    error_message = "Only the documented SNS publishing actions."
  }
}

run "rejects_unexpected_services" {
  command = plan

  variables {
    sns_publishing_services = ["s3"]
  }
  expect_failures = [var.sns_publishing_services]
}
