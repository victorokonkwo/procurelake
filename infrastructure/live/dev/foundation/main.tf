# dev / foundation: encryption key, lakehouse buckets, workload identities, Bronze catalog,
# and the engineering Athena workgroup. Networking is added when RDS/DMS need it (Week 5).

locals {
  name_prefix = "procurelake-dev"
}

module "data_key" {
  source = "../../../modules/kms"

  description = "ProcureLake dev data encryption (S3 lakehouse buckets)"
  alias       = "${local.name_prefix}-data"
}

module "lakehouse" {
  source = "../../../modules/s3_lakehouse"

  name_prefix    = local.name_prefix
  aws_account_id = var.aws_account_id
  region         = var.region
  kms_key_arn    = module.data_key.key_arn
}

module "catalog" {
  source = "../../../modules/glue_catalog"

  name_prefix           = replace(local.name_prefix, "-", "_")
  environment           = "dev"
  raw_bucket_name       = module.lakehouse.bucket_names["raw"]
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
  glue_database_names   = [module.catalog.bronze_database_name]
}
