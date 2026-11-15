mock_provider "aws" {
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:ca-central-1:123456789012:workgroup/procurelake-dev-dbt-cloud" }
  }
  mock_resource "aws_iam_user" {
    defaults = { arn = "arn:aws:iam::123456789012:user/procurelake/procurelake-dev-dbt-cloud" }
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
}

run "builds_only_into_the_sandbox" {
  command = apply

  assert {
    condition     = aws_glue_catalog_database.sandbox.name == "procurelake_dev_dbt_cloud" && aws_glue_catalog_database.sandbox.location_uri == "s3://procurelake-dev-lake-123456789012-ca-central-1/dbt_cloud/"
    error_message = "The sandbox database lives under dbt_cloud/, outside the Lake Formation-registered gold/."
  }
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_user_policy.dbt_cloud.policy).Statement :
      length(regexall("gold|bronze|raw-|products-", jsonencode(s))) == 0
    ])
    error_message = "The dbt Cloud user must not reach Gold, Bronze or the products bucket."
  }
  assert {
    condition = alltrue(flatten([
      for s in jsondecode(aws_iam_user_policy.dbt_cloud.policy).Statement : [
        for r in flatten([s.Resource]) : !startswith(r, "arn:aws:glue") || r == "arn:aws:glue:ca-central-1:123456789012:catalog" || strcontains(r, "procurelake_dev_dbt_cloud") || (strcontains(r, "procurelake_dev_silver") && !contains(flatten([s.Action]), "glue:CreateTable"))
      ]
    ]))
    error_message = "Glue writes are limited to the sandbox database; Silver is read-only."
  }
  assert {
    condition     = output.dbt_cloud_connection.schema == "procurelake_dev_dbt_cloud" && output.dbt_cloud_connection.work_group == "procurelake-dev-dbt-cloud"
    error_message = "The connection output names the sandbox schema and the dbt Cloud workgroup."
  }
}

run "key_works_only_from_dbt_cloud" {
  command = apply

  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_user_policy.dbt_cloud.policy).Statement :
      try(jsonencode([s.Effect, s.Action, s.Resource, s.Condition]), "") == jsonencode([
        "Deny", "*", "*",
        { Bool = { "aws:ViaAWSService" = "false" }, NotIpAddress = { "aws:SourceIp" = var.dbt_cloud_ip_addresses } },
      ])
    ])
    error_message = "Every direct call from outside dbt Cloud's addresses must be denied."
  }
  assert {
    condition     = length(var.dbt_cloud_ip_addresses) == 6
    error_message = "Default: the six us-east-1 multi-tenant addresses dbt publishes."
  }
  assert {
    condition     = aws_iam_user.dbt_cloud.force_destroy
    error_message = "Destroy must remove the hand-made access key with the user."
  }
}

run "kms_only_through_s3" {
  command = apply

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_user_policy.dbt_cloud.policy).Statement :
      s.Condition.StringEquals["kms:ViaService"] == "s3.ca-central-1.amazonaws.com"
      if length(regexall("kms:", jsonencode(s.Action))) > 0
    ])
    error_message = "The data key is usable only through S3."
  }
}

run "rejects_an_open_address_range" {
  command = plan

  variables {
    dbt_cloud_ip_addresses = ["0.0.0.0/0"]
  }

  expect_failures = [var.dbt_cloud_ip_addresses]
}
