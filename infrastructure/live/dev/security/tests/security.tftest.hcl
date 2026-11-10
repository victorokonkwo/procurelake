mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-tenant" }
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

variables {
  aws_account_id       = "123456789012"
  state_bucket         = "procurelake-tfstate-123456789012-ca-central-1"
  data_lake_admin_arns = ["arn:aws:iam::123456789012:user/operator"]
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
    condition     = length(aws_lakeformation_opt_in.tenant) == 2
    error_message = "Only the tenant roles are opted in (hybrid access mode)."
  }
}
