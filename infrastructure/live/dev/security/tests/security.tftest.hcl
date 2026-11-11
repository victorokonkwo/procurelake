mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-tenant" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-tenant-exports" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:ca-central-1:123456789012:workgroup/procurelake-dev-tenant" }
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
      data_kms_key_arn   = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
      gold_database_name = "procurelake_dev_gold"
    }
  }
}

override_data {
  target = data.terraform_remote_state.data_platform
  values = {
    outputs = {
      pipeline_alerts_topic_arn = "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts"
    }
  }
}

variables {
  aws_account_id       = "123456789012"
  state_bucket         = "procurelake-tfstate-123456789012-ca-central-1"
  data_lake_admin_arns = ["arn:aws:iam::123456789012:user/operator"]
  operator_arns        = ["arn:aws:iam::123456789012:user/operator"]
}

run "account_defaults_stay_iam_governed" {
  command = apply

  assert {
    condition     = one(aws_lakeformation_data_lake_settings.this.create_table_default_permissions).principal == "IAM_ALLOWED_PRINCIPALS" && contains(one(aws_lakeformation_data_lake_settings.this.create_table_default_permissions).permissions, "ALL")
    error_message = "New tables (dbt rebuilds) must keep IAM_ALLOWED_PRINCIPALS:ALL or the pipeline loses access."
  }
  assert {
    condition     = one(aws_lakeformation_data_lake_settings.this.create_database_default_permissions).principal == "IAM_ALLOWED_PRINCIPALS"
    error_message = "New databases must keep IAM_ALLOWED_PRINCIPALS."
  }
  assert {
    condition     = aws_lakeformation_resource.gold.hybrid_access_enabled && aws_lakeformation_resource.gold.arn == "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1/gold"
    error_message = "Only the gold/ prefix is registered, in hybrid access mode."
  }
}

run "tenants_have_no_direct_lakehouse_path" {
  command = apply

  assert {
    condition = alltrue([for p in aws_iam_role_policy.tenant : anytrue([for s in jsondecode(p.policy).Statement :
    s.Effect == "Deny" && s.Action == "s3:*" && contains(s.Resource, "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1/*")])])
    error_message = "Every tenant role must be denied direct S3 access to the lakehouse."
  }
  assert {
    condition = alltrue([for p in aws_iam_role_policy.tenant : alltrue([for s in jsondecode(p.policy).Statement :
      s.Effect == "Deny" || alltrue([for r in flatten([s.Resource]) : !startswith(r, "arn:aws:s3:::procurelake-dev-lake") && !startswith(r, "arn:aws:s3:::procurelake-dev-raw")])
    ])])
    error_message = "No allow statement may reach the lake or raw buckets."
  }
}

run "each_tenant_sees_only_its_rows" {
  command = apply

  assert {
    condition     = aws_lakeformation_data_cells_filter.tenant["tenant_alder/mart_awarded_value"].table_data[0].row_filter[0].filter_expression == "tenant_id = 'tenant_alder'"
    error_message = "Alder's filter must select only Alder rows."
  }
  assert {
    condition     = aws_lakeformation_data_cells_filter.tenant["tenant_birch/mart_awarded_value"].table_data[0].row_filter[0].filter_expression == "tenant_id = 'tenant_birch'"
    error_message = "Birch's filter must select only Birch rows."
  }
  assert {
    condition     = alltrue([for g in aws_lakeformation_permissions.tenant_rows : toset(g.permissions) == toset(["SELECT"]) && length(g.permissions_with_grant_option) == 0])
    error_message = "Tenants get SELECT through their filter only, never with grant option."
  }
  assert {
    condition     = alltrue([for o in aws_lakeformation_opt_in.tenant : contains([for r in aws_iam_role.tenant : r.arn], one(o.principal).data_lake_principal_identifier)])
    error_message = "Only the tenant roles are opted in (hybrid access mode)."
  }
}

run "every_tenant_mart_is_filtered_per_tenant" {
  command = plan

  assert {
    condition     = length(aws_lakeformation_data_cells_filter.tenant) == 8 && length(aws_lakeformation_opt_in.tenant) == 8 && length(aws_lakeformation_permissions.tenant_rows) == 8
    error_message = "Each of the 4 marts needs a filter, a filtered SELECT grant and an opt-in for each of the 2 tenants."
  }
  assert {
    condition     = alltrue([for k, f in aws_lakeformation_data_cells_filter.tenant : one(f.table_data).row_filter[0].filter_expression == "tenant_id = '${split("/", k)[0]}'"])
    error_message = "Every filter must restrict rows to its own tenant_id."
  }
}

run "admin_reads_every_mart_unfiltered_with_no_s3_path" {
  command = apply

  assert {
    condition     = length(aws_lakeformation_permissions.admin_tables) == 4 && alltrue([for g in aws_lakeformation_permissions.admin_tables : toset(g.permissions) == toset(["SELECT"]) && length(g.permissions_with_grant_option) == 0])
    error_message = "The admin gets SELECT (no grant option) on each tenant-facing mart."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.admin.policy).Statement :
    s.Effect == "Deny" && s.Action == "s3:*" && contains(s.Resource, "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1/*")])
    error_message = "The admin role must be denied direct S3 access to the lakehouse."
  }
}

run "exports_run_as_each_tenant_into_its_own_prefix" {
  command = apply

  assert {
    condition     = length(local.exports) == 8 && alltrue([for e in local.exports : contains(["tenant_alder", "tenant_birch"], e.tenant)])
    error_message = "One export per tenant per mart."
  }
  assert {
    condition     = strcontains(aws_sfn_state_machine.exports.definition, "\"Credentials\"") && strcontains(aws_sfn_state_machine.exports.definition, "procurelake-dev-products-123456789012-ca-central-1")
    error_message = "Exports must run with the tenant role's credentials and write to the products bucket."
  }
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_role_policy.exports.policy).Statement : s.Action != "s3:*" && !strcontains(jsonencode(s.Action), "s3:")])
    error_message = "The export state machine itself has no S3 access; only the tenant roles write exports."
  }
  assert {
    condition = alltrue([for t, p in aws_iam_role_policy.tenant : anytrue([for s in jsondecode(p.policy).Statement :
    s.Sid == "WriteAndReadOwnExports" && s.Resource == "arn:aws:s3:::procurelake-dev-products-123456789012-ca-central-1/${t}/*"])])
    error_message = "Each tenant role writes exports only under its own prefix."
  }
}

run "access_check_queries_every_mart_as_every_tenant" {
  command = apply

  assert {
    condition     = strcontains(aws_sfn_state_machine.access_check.definition, "\"Credentials\"") && strcontains(aws_sfn_state_machine.access_check.definition, "foreign_rows")
    error_message = "The check must query as each tenant role and count foreign rows."
  }
  assert {
    condition     = alltrue([for p in [aws_iam_role_policy.exports, aws_iam_role_policy.access_check] : anytrue([for s in jsondecode(p.policy).Statement : s.Sid == "EncryptAlert"])])
    error_message = "Publishing to the KMS-encrypted alerts topic needs the data key."
  }
}

run "tenant_and_admin_roles_trust_only_named_principals" {
  command = apply

  assert {
    condition = alltrue([for t in aws_iam_role.tenant :
      toset(jsondecode(t.assume_role_policy).Statement[0].Condition.ArnEquals["aws:PrincipalArn"]) == toset([
        "arn:aws:iam::123456789012:user/operator", aws_iam_role.exports.arn, aws_iam_role.access_check.arn,
    ])])
    error_message = "Tenant roles may be assumed only by the operator and the exports and access-check state machines (ADR 0037)."
  }
  assert {
    condition     = jsondecode(aws_iam_role.admin.assume_role_policy).Statement[0].Condition.ArnEquals["aws:PrincipalArn"] == ["arn:aws:iam::123456789012:user/operator"]
    error_message = "The admin role may be assumed only by the operator."
  }
}
