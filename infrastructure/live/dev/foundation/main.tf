# dev / foundation: encryption key, lakehouse buckets, and workload identities.
# Glue Data Catalog and Athena are added on Sprint 1 Day 5. Networking is added when RDS/DMS
# need it (Week 5).

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

module "iam" {
  source = "../../../modules/iam"

  name_prefix    = local.name_prefix
  aws_account_id = var.aws_account_id
  bucket_arns    = module.lakehouse.bucket_arns
  kms_key_arn    = module.data_key.key_arn
}
