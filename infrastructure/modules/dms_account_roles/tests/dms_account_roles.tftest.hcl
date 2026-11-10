mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

variables {
  aws_account_id = "123456789012"
}

run "roles_have_the_names_dms_requires" {
  command = apply

  assert {
    condition     = aws_iam_role.vpc.name == "dms-vpc-role" && aws_iam_role.cloudwatch_logs.name == "dms-cloudwatch-logs-role"
    error_message = "DMS looks these roles up by exact name."
  }
  assert {
    condition     = jsondecode(aws_iam_role.vpc.assume_role_policy).Statement[0].Principal.Service == "dms.amazonaws.com" && jsondecode(aws_iam_role.vpc.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    error_message = "Only DMS acting for this account may assume the roles."
  }
}
