# dev / cdc: the disposable CDC source and replication (ADR 0027). Billed hourly (RDS
# db.t4g.micro + DMS dms.t3.small + one Secrets Manager interface endpoint): destroy it after
# every working or demo session (`make cdc-down`). Bronze data and Terraform state are kept.
#
# Two phases:
#   1. enable_dms = false: network, RDS and secrets. Then `make cdc-source-setup` migrates and
#      seeds the source and creates the DMS user.
#   2. enable_dms = true: replication instance, endpoints and the full-load-and-CDC task.

locals {
  name          = "procurelake-dev-cdc"
  foundation    = data.terraform_remote_state.foundation.outputs
  cdc_machine   = try(data.terraform_remote_state.data_platform.outputs.cdc_processing_state_machine_arn, null)
  with_schedule = var.enable_dms && local.cdc_machine != null
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

module "network" {
  source = "../../../modules/cdc_network"

  name   = local.name
  region = var.region
}

module "source" {
  source = "../../../modules/rds_postgresql"

  name           = "${local.name}-source"
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.public_subnet_ids
  operator_cidrs = var.operator_cidrs
  kms_key_arn    = local.foundation.data_kms_key_arn
}

module "dms" {
  source = "../../../modules/dms_s3_cdc"
  count  = var.enable_dms ? 1 : 0

  name                   = local.name
  aws_account_id         = var.aws_account_id
  region                 = var.region
  subnet_ids             = module.network.private_subnet_ids
  security_group_id      = module.source.replication_security_group_id
  replication_secret_arn = module.source.replication_secret_arn
  database_name          = module.source.database_name
  raw_bucket_name        = local.foundation.bucket_names["raw"]
  kms_key_arn            = local.foundation.data_kms_key_arn
}

# ----------------------------------------------------------------------------- schedule
# Start cdc_processing every 5 minutes while this stack (and DMS) exists (ADR 0029, owner
# decision). Destroyed with the stack, so nothing runs when CDC is down.
resource "aws_iam_role" "scheduler" {
  count = local.with_schedule ? 1 : 0

  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard (EventBridge Scheduler docs): this account, the default group.
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = var.aws_account_id
          "aws:SourceArn"     = "arn:aws:scheduler:${var.region}:${var.aws_account_id}:schedule-group/default"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  count = local.with_schedule ? 1 : 0

  name = "start-cdc-processing"
  role = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = local.cdc_machine }]
  })
}

resource "aws_scheduler_schedule" "cdc_processing" {
  count = local.with_schedule ? 1 : 0

  name                = "${local.name}-processing"
  description         = "Start CDC processing (CDC apply -> dbt) every 5 minutes while dev/cdc is up."
  schedule_expression = "rate(5 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = local.cdc_machine
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ dms_task_arn = module.dms[0].replication_task_arn })
  }
}
