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
  lake_arn   = "arn:aws:s3:::${local.foundation.bucket_names["lake"]}"
  raw_arn    = "arn:aws:s3:::${local.foundation.bucket_names["raw"]}"
  art_arn    = "arn:aws:s3:::${local.foundation.bucket_names["artifacts"]}"
  prod_arn   = "arn:aws:s3:::${local.foundation.bucket_names["products"]}"
  gold_db    = local.foundation.gold_database_name
  glue       = "arn:aws:glue:${var.region}:${var.aws_account_id}"
  filters    = { for pair in setproduct(var.tenants, var.filtered_tables) : "${pair[0]}/${pair[1]}" => { tenant = pair[0], table = pair[1] } }
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
  admins = var.data_lake_admin_arns

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
  for_each = toset(var.tenants)

  name        = "procurelake-dev-${replace(each.value, "_", "-")}"
  description = "Consumer role for synthetic tenant ${each.value}: Gold through Lake Formation only."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
      Action    = "sts:AssumeRole"
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
        Resource = local.art_arn
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
