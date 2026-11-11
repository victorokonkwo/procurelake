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
      "repo:victorokonkwo@42738051/procurelake@1452285010:pull_request",
      "repo:victorokonkwo@42738051/procurelake@1452285010:ref:refs/heads/dev",
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
    condition     = jsondecode(aws_iam_role.github_deploy_dev.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:victorokonkwo@42738051/procurelake@1452285010:environment:dev"
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

run "deploy_role_is_scoped_not_admin" {
  command = apply

  assert {
    condition     = aws_iam_role_policy_attachment.github_deploy_dev_readonly.policy_arn == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    error_message = "The deploy role reads through ReadOnlyAccess; AdministratorAccess is gone (ADR 0036)."
  }
  assert {
    condition     = length(regexall("AdministratorAccess", aws_iam_role_policy.github_deploy_dev.policy)) == 0
    error_message = "No statement may grant or attach AdministratorAccess."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.github_deploy_dev.policy).Statement :
    s.Effect == "Deny" && s.Action == "iam:AttachRolePolicy" && length(s.Condition.ArnNotEquals["iam:PolicyARN"]) == 2])
    error_message = "Only the two DMS service-role policies may be attached."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.github_deploy_dev.policy).Statement :
    s.Effect == "Deny" && s.Action == "iam:*" && s.Resource == "arn:aws:iam::123456789012:role/procurelake-github-*"])
    error_message = "The deploy role must not be able to change the CI roles."
  }
  assert {
    condition = alltrue(flatten([for s in jsondecode(aws_iam_role_policy.github_deploy_dev.policy).Statement : [
      for r in flatten([try(s.Resource, [])]) : s.Effect == "Deny" || !startswith(r, "arn:aws:iam::") || strcontains(r, ":role/procurelake-dev-") || strcontains(r, ":role/dms-")
    ]]))
    error_message = "IAM writes are limited to procurelake-dev-* roles and the two DMS roles."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.github_deploy_dev.policy).Statement :
    s.Effect == "Deny" && contains(flatten([s.Action]), "s3:GetObject") && contains(flatten([s.Action]), "s3:DeleteObject")])
    error_message = "The deploy role must never read, write or delete lake data objects."
  }
}
