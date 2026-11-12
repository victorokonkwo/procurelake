mock_provider "aws" {}

variables {
  name_prefix    = "procurelake-dev"
  aws_account_id = "123456789012"
  kms_key_arn    = "arn:aws:kms:ca-central-1:123456789012:key/test"
  bucket_arns = {
    raw       = "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1"
    lake      = "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1"
    products  = "arn:aws:s3:::procurelake-dev-products-123456789012-ca-central-1"
    artifacts = "arn:aws:s3:::procurelake-dev-artifacts-123456789012-ca-central-1"
  }
}

run "roles_are_separated_and_trust_only_this_account" {
  command = apply

  assert {
    condition     = aws_iam_role.ingestion.name == "procurelake-dev-ingestion" && aws_iam_role.data_engineer.name == "procurelake-dev-data-engineer"
    error_message = "Unexpected role names."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::123456789012:root" &&
      length(jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement) == 1
    )
    error_message = "Without service principals, only this account may assume the role."
  }
}

run "nobody_may_delete_bronze" {
  command = apply

  assert {
    condition = alltrue([for p in [aws_iam_role_policy.ingestion.policy, aws_iam_role_policy.data_engineer.policy] :
      anytrue([for s in jsondecode(p).Statement :
        s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Action, "s3:DeleteObjectVersion") &&
    s.Resource == "${var.bucket_arns.raw}/*"])])
    error_message = "Both roles must explicitly deny deleting Bronze objects and versions."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.ingestion.policy).Statement :
    s.Effect == "Deny" || !contains(flatten([s.Action]), "s3:DeleteObject")])
    error_message = "Ingestion must not be allowed to delete objects anywhere."
  }
}

run "ingestion_writes_only_landing_prefixes_and_manifests" {
  command = apply

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.ingestion.policy).Statement :
      s.Resource if s.Effect == "Allow" && contains(flatten([s.Action]), "s3:PutObject")])) == toset([
      "${var.bucket_arns.raw}/bronze/*",
      "${var.bucket_arns.raw}/documents/*",
      "${var.bucket_arns.raw}/quarantine/*",
      "${var.bucket_arns.artifacts}/pipeline_manifests/*",
    ])
    error_message = "Ingestion PutObject must be limited to Bronze landing prefixes and manifests."
  }
}

run "data_engineer_cannot_write_bronze" {
  command = apply

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.data_engineer.policy).Statement :
      !(s.Effect == "Allow" && contains(flatten([s.Action]), "s3:PutObject") &&
    anytrue([for r in flatten([s.Resource]) : startswith(r, var.bucket_arns.raw)]))])
    error_message = "Data engineer must not be able to write to the raw bucket."
  }
}

run "service_principals_are_account_scoped" {
  command = apply

  variables {
    trusted_service_principals = ["glue.amazonaws.com"]
  }

  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement :
    try(s.Principal.Service[0], "") == "glue.amazonaws.com" && s.Condition.StringEquals["aws:SourceAccount"] == "123456789012"])
    error_message = "Service principal trust must be limited by aws:SourceAccount."
  }
}

run "requires_all_bucket_roles" {
  command = plan

  variables {
    bucket_arns = { raw = "arn:aws:s3:::x" }
  }
  expect_failures = [var.bucket_arns]
}

run "query_access_is_data_engineer_only_and_scoped" {
  command = apply

  variables {
    athena_workgroup_arns = ["arn:aws:athena:ca-central-1:123456789012:workgroup/procurelake-dev-engineering"]
    glue_database_names   = ["procurelake_dev_bronze"]
  }

  assert {
    condition     = length(aws_iam_role_policy.data_engineer_query) == 1 && aws_iam_role_policy.data_engineer_query[0].role == aws_iam_role.data_engineer.id
    error_message = "Query access must be attached to the data engineer role."
  }
  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.data_engineer_query[0].policy).Statement :
      s.Resource if s.Sid == "RunAthenaQueriesInPlatformWorkgroups"])) == toset([
      "arn:aws:athena:ca-central-1:123456789012:workgroup/procurelake-dev-engineering",
    ])
    error_message = "Athena actions must be limited to the platform workgroup."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.data_engineer_query[0].policy).Statement :
    alltrue([for a in flatten([s.Action]) : !can(regex("^glue:(Create|Update|Delete|Put|BatchCreate|BatchDelete)", a))])])
    error_message = "The data engineer must not be able to modify the catalog through this policy."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.ingestion.policy).Statement :
    alltrue([for a in flatten([s.Action]) : !startswith(a, "athena:") && !startswith(a, "glue:")])])
    error_message = "Ingestion must not have Athena or Glue access."
  }
}

run "no_query_policy_without_workgroups" {
  command = apply

  assert {
    condition     = length(aws_iam_role_policy.data_engineer_query) == 0
    error_message = "Query policy must not exist when no workgroups are given."
  }
}

run "build_access_is_limited_to_writable_databases" {
  command = apply

  variables {
    glue_writable_database_names = ["procurelake_dev_gold"]
  }

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.data_engineer_build[0].policy).Statement : s.Resource])) == toset([
      "arn:aws:glue:ca-central-1:123456789012:catalog",
      "arn:aws:glue:ca-central-1:123456789012:database/procurelake_dev_gold",
      "arn:aws:glue:ca-central-1:123456789012:table/procurelake_dev_gold/*",
    ])
    error_message = "Catalog writes must be limited to the Gold databases (never Bronze or Silver)."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.data_engineer_build[0].policy).Statement :
    !contains(flatten([s.Action]), "glue:CreateDatabase") && !contains(flatten([s.Action]), "glue:DeleteDatabase")])
    error_message = "Databases are owned by Terraform, not dbt."
  }
}

run "no_build_policy_without_writable_databases" {
  command = apply

  assert {
    condition     = length(aws_iam_role_policy.data_engineer_build) == 0
    error_message = "Build policy must not exist when no writable databases are given."
  }
}

run "only_the_data_engineer_trusts_ecs_tasks" {
  command = apply

  variables {
    data_engineer_service_principals = ["ecs-tasks.amazonaws.com"]
  }

  assert {
    condition     = contains(flatten([for st in jsondecode(aws_iam_role.data_engineer.assume_role_policy).Statement : flatten([try(st.Principal.Service, [])])]), "ecs-tasks.amazonaws.com")
    error_message = "The data engineer must be assumable by ECS tasks (dbt runner)."
  }
  assert {
    condition     = !contains(flatten([for st in jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement : flatten([try(st.Principal.Service, [])])]), "ecs-tasks.amazonaws.com")
    error_message = "The ingestion role must not trust ECS tasks."
  }
  assert {
    condition     = alltrue([for st in jsondecode(aws_iam_role.data_engineer.assume_role_policy).Statement : !can(st.Principal.Service) || st.Condition.StringEquals["aws:SourceAccount"] == "123456789012"])
    error_message = "Service trust must be limited to this account."
  }
}

run "ingestion_trusts_ecs_tasks_only_when_configured_and_account_scoped" {
  command = apply

  variables {
    ingestion_service_principals = ["ecs-tasks.amazonaws.com"]
  }

  assert {
    condition     = contains(flatten([for st in jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement : flatten([try(st.Principal.Service, [])])]), "ecs-tasks.amazonaws.com")
    error_message = "The ingestion role must be assumable by ECS tasks when configured (batch ingestion, ADR 0040)."
  }
  assert {
    condition     = !contains(flatten([for st in jsondecode(aws_iam_role.data_engineer.assume_role_policy).Statement : flatten([try(st.Principal.Service, [])])]), "ecs-tasks.amazonaws.com")
    error_message = "Ingestion's service principals must not leak into the data engineer's trust."
  }
  assert {
    condition     = alltrue([for st in jsondecode(aws_iam_role.ingestion.assume_role_policy).Statement : !can(st.Principal.Service) || st.Condition.StringEquals["aws:SourceAccount"] == "123456789012"])
    error_message = "Service trust must be limited to this account."
  }
}

run "ingestion_may_abort_only_its_bronze_uploads" {
  command = apply

  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.ingestion.policy).Statement :
    s.Resource if s.Effect == "Allow" && contains(flatten([s.Action]), "s3:AbortMultipartUpload")])) == toset(["${var.bucket_arns.raw}/bronze/*"])
    error_message = "AbortMultipartUpload must be limited to the Bronze prefix."
  }
}
