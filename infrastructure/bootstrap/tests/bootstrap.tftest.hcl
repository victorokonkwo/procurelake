mock_provider "aws" {}

variables {
  aws_account_id      = "123456789012"
  budget_alert_emails = ["alerts@example.com"]
}

run "state_bucket_is_hardened" {
  command = apply

  assert {
    condition     = aws_s3_bucket.state.bucket == "procurelake-tfstate-123456789012-ca-central-1"
    error_message = "Unexpected state bucket name."
  }
  assert {
    condition     = aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "State bucket must be versioned."
  }
  assert {
    condition = (
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets
    )
    error_message = "State bucket must block all public access."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms"
    error_message = "State must be KMS-encrypted."
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    error_message = "State bucket must deny non-TLS access."
  }
  assert {
    condition     = strcontains(output.backend_hcl, "use_lockfile = true")
    error_message = "Backend config must enable native S3 locking."
  }
}

run "github_plan_role_trusts_only_prs_and_dev_branch" {
  command = apply

  assert {
    condition = toset(jsondecode(aws_iam_role.github_plan.assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"]) == toset([
      "repo:victorokonkwo/procurelake:pull_request",
      "repo:victorokonkwo/procurelake:ref:refs/heads/dev",
    ])
    error_message = "Plan role must trust only pull requests and the dev branch of this repo."
  }
  assert {
    condition     = jsondecode(aws_iam_role.github_plan.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "Plan role must require the sts.amazonaws.com audience."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.github_plan.policy).Statement :
      s.Effect == "Deny" && contains(s.Action, "s3:GetObject") && toset(s.NotResource) == toset([
        "${aws_s3_bucket.state.arn}/*",
        "arn:aws:s3:::procurelake-dev-artifacts-123456789012-ca-central-1/glue/*",
    ])])
    error_message = "Plan role must be denied reading any S3 object except Terraform state and the deployed Glue code."
  }
}

run "github_deploy_role_trusts_only_dev_environment" {
  command = apply

  assert {
    condition     = jsondecode(aws_iam_role.github_deploy_dev.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:victorokonkwo/procurelake:environment:dev"
    error_message = "Deploy role must trust only the GitHub 'dev' environment."
  }
}

run "budget_exists_with_requested_limit" {
  command = apply

  assert {
    condition     = module.budget.budget_name == "procurelake-monthly-cost"
    error_message = "Budget must be created by the bootstrap stack."
  }
}
