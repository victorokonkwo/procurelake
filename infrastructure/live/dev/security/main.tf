# dev / security: tenant isolation with Lake Formation on Gold (ADR 0030, work package D).
#
# s3://<lake>/gold is registered in HYBRID access mode: only opted-in principals (the tenant
# roles) are governed by Lake Formation; everyone else (dbt, Glue) keeps IAM access, so dbt's
# CTAS/MERGE still work on the registered location (live experiment, 2026-11-10; evidence in
# docs/evidence/sprint1-week7-lake-formation-experiment.md).
#
# Lake Formation deletes a table's filters, grants and opt-ins when the table is dropped, and
# a dbt `table` rebuild drops it. Tenant-facing marts are therefore incremental merges that
# are never dropped. After a deliberate full refresh, re-apply this root (-parallelism=1).
#
#   - Data lake admins: var.data_lake_admin_arns. The defaults for new databases/tables stay
#     IAM_ALLOWED_PRINCIPALS:ALL and the parameters are kept, so tables dbt (re)creates keep
#     IAM access for the pipeline (values read from the account before this change).
#   - Registration role: custom (the buckets use a customer-managed KMS key).
#   - Tenant roles: no S3 access to the lakehouse at all (explicit deny); data comes only
#     through Lake Formation; query results only in their own Athena workgroup prefix.

locals {
  foundation = data.terraform_remote_state.foundation.outputs
  alerts_arn = data.terraform_remote_state.data_platform.outputs.pipeline_alerts_topic_arn
  lake_arn   = "arn:aws:s3:::${local.foundation.bucket_names["lake"]}"
  raw_arn    = "arn:aws:s3:::${local.foundation.bucket_names["raw"]}"
  art_arn    = "arn:aws:s3:::${local.foundation.bucket_names["artifacts"]}"
  prod_arn   = "arn:aws:s3:::${local.foundation.bucket_names["products"]}"
  gold_db    = local.foundation.gold_database_name
  glue       = "arn:aws:glue:${var.region}:${var.aws_account_id}"
  filters    = { for pair in setproduct(var.tenants, var.filtered_tables) : "${pair[0]}/${pair[1]}" => { tenant = pair[0], table = pair[1] } }
}

data "terraform_remote_state" "data_platform" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/data_platform/terraform.tfstate"
    region = var.region
  }
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/foundation/terraform.tfstate"
    region = var.region
  }
}

# ----------------------------------------------------------------------------- settings
resource "aws_lakeformation_data_lake_settings" "this" {
  admins           = var.data_lake_admin_arns
  read_only_admins = var.data_lake_read_only_admin_arns

  # Unchanged account defaults (get-data-lake-settings, 2026-11-10): new databases and tables
  # stay IAM-governed, so dbt/Glue keep working on tables they create.
  create_database_default_permissions {
    permissions = ["ALL"]
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }
  create_table_default_permissions {
    permissions = ["ALL"]
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }

  parameters = {
    CROSS_ACCOUNT_VERSION = "4"
    SET_CONTEXT           = "TRUE"
  }
}

# ----------------------------------------------------------------------------- registration
resource "aws_iam_role" "lf_registration" {
  name        = "procurelake-dev-lf-gold-access"
  description = "Lake Formation reads Gold on behalf of governed (opted-in) principals."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lakeformation.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:SetContext"]
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "lf_registration" {
  name = "gold-read-write"
  role = aws_iam_role.lf_registration.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListGold"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["gold/*", "gold"] } }
      },
      {
        # The documented registration-role policy grants read/write on the location.
        Sid      = "ReadWriteGold"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${local.lake_arn}/gold/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
    ]
  })
}

resource "aws_lakeformation_resource" "gold" {
  arn                   = "${local.lake_arn}/gold"
  role_arn              = aws_iam_role.lf_registration.arn
  hybrid_access_enabled = true

  depends_on = [aws_lakeformation_data_lake_settings.this, aws_iam_role_policy.lf_registration]
}

# ----------------------------------------------------------------------------- tenants
module "tenant_workgroup" {
  source   = "../../../modules/athena"
  for_each = toset(var.tenants)

  name                = "procurelake-dev-${replace(each.value, "_", "-")}"
  description         = "Athena workgroup for synthetic tenant ${each.value} (results encrypted, 1 GiB cutoff)."
  results_bucket_name = local.foundation.bucket_names["artifacts"]
  kms_key_arn         = local.foundation.data_kms_key_arn
}

resource "aws_iam_role" "tenant" {
  # checkov:skip=CKV_AWS_61: trust limited by an aws:PrincipalArn condition to the operator and two state machine roles (ADR 0037); Checkov ignores conditions
  for_each = toset(var.tenants)

  name        = "procurelake-dev-${replace(each.value, "_", "-")}"
  description = "Consumer role for synthetic tenant ${each.value}: Gold through Lake Formation only."
  # Only the operator (tests, drills) and the two state machines that act as a tenant (daily
  # exports, the access check) may assume it, not any principal in the account (ADR 0037).
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = { ArnEquals = { "aws:PrincipalArn" = concat(var.operator_arns, [aws_iam_role.exports.arn, aws_iam_role.access_check.arn]) } }
    }]
  })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "tenant" {
  for_each = toset(var.tenants)

  name = "tenant-query"
  role = aws_iam_role.tenant[each.value].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "QueryInOwnWorkgroup"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution", "athena:StopQueryExecution", "athena:GetQueryExecution",
          "athena:GetQueryResults", "athena:GetWorkGroup",
        ]
        Resource = module.tenant_workgroup[each.value].workgroup_arn
      },
      {
        Sid      = "ReadGoldCatalog"
        Effect   = "Allow"
        Action   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"]
        Resource = ["${local.glue}:catalog", "${local.glue}:database/${local.gold_db}", "${local.glue}:table/${local.gold_db}/*"]
      },
      {
        # Lake Formation vends the data; cell-level filtered queries need the planning actions.
        Sid    = "LakeFormationDataAccess"
        Effect = "Allow"
        Action = [
          "lakeformation:GetDataAccess", "lakeformation:StartQueryPlanning", "lakeformation:GetQueryState",
          "lakeformation:GetWorkUnits", "lakeformation:GetWorkUnitResults",
        ]
        Resource = "*"
      },
      {
        Sid      = "OwnQueryResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.art_arn}/athena_results/procurelake-dev-${replace(each.value, "_", "-")}/*"
      },
      {
        Sid       = "ListOwnQueryResults"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["athena_results/procurelake-dev-${replace(each.value, "_", "-")}/*"] } }
      },
      {
        # GetBucketLocation carries no s3:prefix key, so under the condition above it was always
        # denied, and Athena failed "Unable to verify/create output bucket" (live, 2026-11-10).
        Sid      = "LocateResultsBucket"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = [local.art_arn, local.prod_arn]
      },
      {
        # Daily exports (tenant_exports state machine) run UNLOAD as this role, so the export
        # gets the same row filter as the tenant's queries. The products bucket policy denies
        # every other prefix and all deletes to this role (foundation, ADR 0030).
        Sid      = "WriteAndReadOwnExports"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.prod_arn}/${each.value}/*"
      },
      {
        Sid       = "ListOwnExports"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource  = local.prod_arn
        Condition = { StringLike = { "s3:prefix" = ["${each.value}/*"] } }
      },
      {
        Sid      = "EncryptQueryResults"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:Encrypt"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        # Lake Formation filters do not cover direct S3 reads: deny every path to the lakehouse.
        Sid      = "DenyDirectLakehouseAccess"
        Effect   = "Deny"
        Action   = "s3:*"
        Resource = [local.lake_arn, "${local.lake_arn}/*", local.raw_arn, "${local.raw_arn}/*"]
      },
    ]
  })
}

# ----------------------------------------------------------------------------- filters + grants
resource "aws_lakeformation_data_cells_filter" "tenant" {
  for_each = local.filters

  table_data {
    table_catalog_id = var.aws_account_id
    database_name    = local.gold_db
    table_name       = each.value.table
    name             = "${each.value.tenant}_rows"

    row_filter {
      filter_expression = "tenant_id = '${each.value.tenant}'"
    }
    # Explicit empty list: with the block left empty the provider reports an inconsistent
    # result after apply (null planned, empty set returned; first dev apply, 2026-11-10).
    column_wildcard {
      excluded_column_names = []
    }
  }

  depends_on = [aws_lakeformation_data_lake_settings.this, aws_lakeformation_resource.gold]
}

resource "aws_lakeformation_permissions" "tenant_database" {
  for_each = toset(var.tenants)

  principal   = aws_iam_role.tenant[each.value].arn
  permissions = ["DESCRIBE"]
  database {
    name = local.gold_db
  }
  depends_on = [aws_lakeformation_data_lake_settings.this]

  # ListPermissions filtered to this principal returns ["ALL", "DESCRIBE"], although the
  # unfiltered listing of the database shows the role holding only DESCRIBE (checked live,
  # 2026-11-10). The provider reads the filtered view, so every plan wanted to replace the
  # grant. Matching it would mean granting ALL, so the drift is ignored instead. Side effect:
  # a change to `permissions` here needs `-replace` to take effect.
  lifecycle {
    ignore_changes = [permissions]
  }
}

resource "aws_lakeformation_permissions" "tenant_rows" {
  for_each = local.filters

  principal                     = aws_iam_role.tenant[each.value.tenant].arn
  permissions                   = ["SELECT"]
  permissions_with_grant_option = [] # tenants can never re-grant
  data_cells_filter {
    table_catalog_id = var.aws_account_id
    database_name    = local.gold_db
    table_name       = each.value.table
    name             = aws_lakeformation_data_cells_filter.tenant[each.key].table_data[0].name
  }
}

# Hybrid access mode: only these principals, on these tables, are governed by Lake Formation.
# Lake Formation rejects simultaneous opt-in updates with ConcurrentModificationException
# (live, 2026-11-10: one of two parallel creates failed). Apply this root with -parallelism=1.
resource "aws_lakeformation_opt_in" "tenant" {
  for_each = local.filters

  principal {
    data_lake_principal_identifier = aws_iam_role.tenant[each.value.tenant].arn
  }
  resource_data {
    table {
      catalog_id    = var.aws_account_id
      database_name = local.gold_db
      name          = each.value.table
    }
  }
  depends_on = [aws_lakeformation_permissions.tenant_rows]
}

# ----------------------------------------------------------------------------- admin product
# Internal cross-tenant analytics for authorised administrators (work package D2): the same
# marts, every tenant's rows, through Lake Formation (SELECT on the table, no filter). Like the
# tenants, no direct S3 path to the lakehouse and a dedicated Athena workgroup.
module "admin_workgroup" {
  source = "../../../modules/athena"

  name                = "procurelake-dev-analytics-admin"
  description         = "Athena workgroup for cross-tenant analytics administrators (results encrypted, 1 GiB cutoff)."
  results_bucket_name = local.foundation.bucket_names["artifacts"]
  kms_key_arn         = local.foundation.data_kms_key_arn
}

resource "aws_iam_role" "admin" {
  # checkov:skip=CKV_AWS_61: trust limited by an aws:PrincipalArn condition to the operator (ADR 0037); Checkov ignores conditions
  name        = "procurelake-dev-analytics-admin"
  description = "Cross-tenant analytics administrator: tenant-facing Gold marts, all tenants, through Lake Formation only."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = { ArnEquals = { "aws:PrincipalArn" = var.operator_arns } } # ADR 0037
    }]
  })
}

resource "aws_iam_role_policy" "admin" {
  name = "admin-query"
  role = aws_iam_role.admin.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "QueryInOwnWorkgroup"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution", "athena:StopQueryExecution", "athena:GetQueryExecution",
          "athena:GetQueryResults", "athena:GetWorkGroup",
        ]
        Resource = module.admin_workgroup.workgroup_arn
      },
      {
        Sid      = "ReadGoldCatalog"
        Effect   = "Allow"
        Action   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"]
        Resource = ["${local.glue}:catalog", "${local.glue}:database/${local.gold_db}", "${local.glue}:table/${local.gold_db}/*"]
      },
      {
        Sid      = "LakeFormationDataAccess"
        Effect   = "Allow"
        Action   = ["lakeformation:GetDataAccess"]
        Resource = "*"
      },
      {
        Sid      = "OwnQueryResults"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.art_arn}/athena_results/procurelake-dev-analytics-admin/*"
      },
      {
        Sid       = "ListOwnQueryResults"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["athena_results/procurelake-dev-analytics-admin/*"] } }
      },
      {
        Sid      = "LocateResultsBucket"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = local.art_arn
      },
      {
        Sid      = "EncryptQueryResults"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:Encrypt"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid      = "DenyDirectLakehouseAccess"
        Effect   = "Deny"
        Action   = "s3:*"
        Resource = [local.lake_arn, "${local.lake_arn}/*", local.raw_arn, "${local.raw_arn}/*"]
      },
    ]
  })
}

resource "aws_lakeformation_permissions" "admin_database" {
  principal   = aws_iam_role.admin.arn
  permissions = ["DESCRIBE"]
  database {
    name = local.gold_db
  }
  depends_on = [aws_lakeformation_data_lake_settings.this]

  lifecycle {
    ignore_changes = [permissions] # same provider read quirk as tenant_database
  }
}

resource "aws_lakeformation_permissions" "admin_tables" {
  for_each = toset(var.filtered_tables)

  principal                     = aws_iam_role.admin.arn
  permissions                   = ["SELECT"]
  permissions_with_grant_option = []
  table {
    database_name = local.gold_db
    name          = each.value
  }
  depends_on = [aws_lakeformation_data_lake_settings.this]

  # Same provider read quirk as tenant_database: the principal-filtered ListPermissions adds
  # ALL (from IAM_ALLOWED_PRINCIPALS on the table); the unfiltered listing shows SELECT only
  # (checked live, 2026-11-10).
  lifecycle {
    ignore_changes = [permissions]
  }
}

resource "aws_lakeformation_opt_in" "admin" {
  for_each = toset(var.filtered_tables)

  principal {
    data_lake_principal_identifier = aws_iam_role.admin.arn
  }
  resource_data {
    table {
      catalog_id    = var.aws_account_id
      database_name = local.gold_db
      name          = each.value
    }
  }
  depends_on = [aws_lakeformation_permissions.admin_tables, aws_lakeformation_opt_in.tenant]
}

# ----------------------------------------------------------------------------- daily exports
# tenant_exports (orchestration/step_functions/tenant_exports.asl.json): each export task runs
# with the tenant role's credentials (Task `Credentials`), so this role only assumes them.
locals {
  exports_machine = "procurelake-dev-tenant-exports"
  exports = [for k, f in local.filters : {
    tenant    = f.tenant
    mart      = f.table
    role_arn  = aws_iam_role.tenant[f.tenant].arn
    workgroup = module.tenant_workgroup[f.tenant].workgroup_name
  }]
}

resource "aws_iam_role" "exports" {
  name = "${local.exports_machine}-sfn"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "exports" {
  name = "run-tenant-exports"
  role = aws_iam_role.exports.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ActAsTenants", Effect = "Allow", Action = "sts:AssumeRole", Resource = [for r in aws_iam_role.tenant : r.arn] },
      { Sid = "Alert", Effect = "Allow", Action = "sns:Publish", Resource = local.alerts_arn },
      # The alerts topic is encrypted with the data key; publishing needs it.
      { Sid = "EncryptAlert", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = local.foundation.data_kms_key_arn },
    ]
  })
}

resource "aws_sfn_state_machine" "exports" {
  name     = local.exports_machine
  role_arn = aws_iam_role.exports.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/tenant_exports.asl.json", {
    exports_json     = jsonencode(local.exports)
    gold_database    = local.gold_db
    products_bucket  = local.foundation.bucket_names["products"]
    alerts_topic_arn = local.alerts_arn
  })
  depends_on = [aws_iam_role_policy.exports]
}

resource "aws_iam_role" "exports_scheduler" {
  name = "${local.exports_machine}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.aws_account_id
          "aws:SourceArn"     = "arn:aws:scheduler:${var.region}:${var.aws_account_id}:schedule-group/default"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "exports_scheduler" {
  name = "start-tenant-exports"
  role = aws_iam_role.exports_scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.exports.arn }]
  })
}

resource "aws_scheduler_schedule" "exports" {
  name                         = local.exports_machine
  description                  = "Daily tenant Parquet exports (owner decision, 2026-11-10)."
  schedule_expression          = var.exports_schedule
  schedule_expression_timezone = "America/Toronto"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.exports.arn
    role_arn = aws_iam_role.exports_scheduler.arn
    input    = jsonencode({})
  }
}

# ----------------------------------------------------------------------------- access check
# tenant_access_check (orchestration/step_functions/tenant_access_check.asl.json): the last step
# of every dbt_transformation run (dev/data_platform calls it by name). Queries every
# tenant-facing mart as every tenant and fails with an SNS alert if access was lost (e.g. a
# full refresh dropped the grants) or another tenant's rows are visible (ADR 0030).
resource "aws_iam_role" "access_check" {
  name = "procurelake-dev-tenant-access-check-sfn"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "access_check" {
  name = "check-tenant-access"
  role = aws_iam_role.access_check.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ActAsTenants", Effect = "Allow", Action = "sts:AssumeRole", Resource = [for r in aws_iam_role.tenant : r.arn] },
      { Sid = "Alert", Effect = "Allow", Action = "sns:Publish", Resource = local.alerts_arn },
      { Sid = "EncryptAlert", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = local.foundation.data_kms_key_arn },
    ]
  })
}

resource "aws_sfn_state_machine" "access_check" {
  name     = "procurelake-dev-tenant-access-check"
  role_arn = aws_iam_role.access_check.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/tenant_access_check.asl.json", {
    checks_json      = jsonencode(local.exports)
    gold_database    = local.gold_db
    alerts_topic_arn = local.alerts_arn
  })
  depends_on = [aws_iam_role_policy.access_check]
}
