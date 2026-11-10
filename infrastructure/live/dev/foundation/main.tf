# dev / foundation: encryption key, lakehouse buckets, workload identities, Bronze and Silver
# catalog databases, and the engineering Athena workgroup. Glue jobs live in dev/data_platform.
# Networking is added when RDS/DMS need it (Week 5).

locals {
  name_prefix = "procurelake-dev"
}

module "data_key" {
  source = "../../../modules/kms"

  description = "ProcureLake dev data encryption (S3 lakehouse buckets)"
  alias       = "${local.name_prefix}-data"

  # CloudWatch alarms and EventBridge rules publish to the KMS-encrypted alerts topic (Week 8).
  sns_publishing_services = ["cloudwatch", "events"]
}

module "lakehouse" {
  source = "../../../modules/s3_lakehouse"

  name_prefix    = local.name_prefix
  aws_account_id = var.aws_account_id
  region         = var.region
  kms_key_arn    = module.data_key.key_arn

  # Synthetic tenants' export prefixes (ADR 0030); roles are created in dev/security.
  products_tenant_prefixes = {
    for t in var.tenants : t => "arn:aws:iam::${var.aws_account_id}:role/${local.name_prefix}-${replace(t, "_", "-")}"
  }
}

module "catalog" {
  source = "../../../modules/glue_catalog"

  name_prefix           = replace(local.name_prefix, "-", "_")
  environment           = "dev"
  raw_bucket_name       = module.lakehouse.bucket_names["raw"]
  lake_bucket_name      = module.lakehouse.bucket_names["lake"]
  artifacts_bucket_name = module.lakehouse.bucket_names["artifacts"]
}

module "athena" {
  source = "../../../modules/athena"

  name                = "${local.name_prefix}-engineering"
  description         = "ProcureLake dev engineering queries (results encrypted, 1 GiB scan cutoff)."
  results_bucket_name = module.lakehouse.bucket_names["artifacts"]
  kms_key_arn         = module.data_key.key_arn

  named_queries = {
    "bronze-usaspending-awards-flattened" = {
      description = "One row per USAspending award record in Bronze, with its batch taken from the file path."
      database    = module.catalog.bronze_database_name
      query       = file("${path.module}/queries/bronze_usaspending_awards_flattened.sql")
    }
  }
}

module "iam" {
  source = "../../../modules/iam"

  name_prefix           = local.name_prefix
  aws_account_id        = var.aws_account_id
  region                = var.region
  bucket_arns           = module.lakehouse.bucket_arns
  kms_key_arn           = module.data_key.key_arn
  athena_workgroup_arns = [module.athena.workgroup_arn]
  glue_database_names = [
    module.catalog.bronze_database_name,
    module.catalog.silver_database_name,
    module.catalog.gold_database_name,
    module.catalog.gold_staging_database_name,
  ]
  glue_writable_database_names = [module.catalog.gold_database_name, module.catalog.gold_staging_database_name]
  # The dbt runner (ECS Fargate, ADR 0029) runs as the data engineer, like `make dbt-build`.
  data_engineer_service_principals = ["ecs-tasks.amazonaws.com"]
}

# Roles AWS DMS requires by exact name (CDC, ADR 0027). Free; kept here so the disposable
# dev/cdc stack never has to create them.
module "dms_account_roles" {
  source = "../../../modules/dms_account_roles"

  aws_account_id = var.aws_account_id
}
