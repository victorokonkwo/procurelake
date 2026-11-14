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

# ----------------------------------------------------------------------------- DMS monitoring
# Week 8 (implementation plan E): a stopped or stalled DMS task is a *silent* failure. The CDC
# schedule keeps succeeding with nothing new to apply while Silver and Gold go stale. Three
# signals reach the alerts topic (its KMS key and policy allow CloudWatch and EventBridge, for
# alarms and rules named procurelake-dev-*):
#
#   - CDCLatencySource above 5 minutes while the task publishes metrics (a running task that
#     lags). Missing data is "missing" (ADR 0050, open question 39b): the alarm no longer fires
#     at stack creation, before the new task has published anything, and a stopped task leaves
#     it in INSUFFICIENT_DATA (no notification) instead of ALARM.
#   - A dedicated rule for the task stopping or failing (ADR 0050, open question 39a). In the
#     Week 8 drill the EventBridge stop event arrived within a minute, against 11.8 minutes for
#     the latency alarm, so it is now the alarm for a silent stop.
#   - Every replication task state change (EventBridge, informational), kept as the fallback in
#     case the stopped rule's event fields do not match (see below).
#
# DMS delivers its EventBridge events on a best-effort basis (EventBridge events reference).
# They live here so they exist only while the task does (an absent task must not alarm).
locals {
  alerts_arn = try(data.terraform_remote_state.data_platform.outputs.pipeline_alerts_topic_arn, null)
  monitor    = var.enable_dms && local.alerts_arn != null
}

resource "aws_cloudwatch_metric_alarm" "dms_source_latency" {
  count = local.monitor ? 1 : 0

  alarm_name          = "${local.name}-dms-source-latency"
  alarm_description   = "DMS is running but has not captured source changes for over 5 minutes; Gold goes stale silently. Missing data is not an alarm (no notification at stack creation): a stopped task shows INSUFFICIENT_DATA here and alerts through the rule procurelake-dev-cdc-dms-task-stopped (ADR 0050). Runbook: docs/runbooks/cdc-dms-stall.md"
  namespace           = "AWS/DMS"
  metric_name         = "CDCLatencySource"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 300
  treat_missing_data  = "missing"
  dimensions          = module.dms[0].cloudwatch_task_dimensions
  alarm_actions       = [local.alerts_arn]
  ok_actions          = [local.alerts_arn]
}

resource "aws_cloudwatch_event_rule" "dms_task_state" {
  count = local.monitor ? 1 : 0

  name        = "${local.name}-dms-task-state"
  description = "Replication task state changes and failures of the dev CDC task, to the alerts topic."
  event_pattern = jsonencode({
    source        = ["aws.dms"]
    "detail-type" = ["DMS Replication Task State Change"]
    resources     = [module.dms[0].replication_task_arn]
  })
}

resource "aws_cloudwatch_event_target" "dms_task_state" {
  count = local.monitor ? 1 : 0

  rule = aws_cloudwatch_event_rule.dms_task_state[0].name
  arn  = local.alerts_arn

  input_transformer {
    input_paths = {
      event   = "$.detail.eventType"
      message = "$.detail.detailMessage"
      time    = "$.time"
      task    = "$.resources[0]"
    }
    input_template = "\"ProcureLake dev CDC: DMS task event <event> at <time>: <message> (task <task>). Runbook: docs/runbooks/cdc-dms-stall.md\""
  }
}

# Open question 39a (owner decision 2026-11-14, ADR 0050): alarm on the task stopping or
# failing. The AWS DMS User Guide ("Working with Amazon EventBridge events and notifications")
# documents the event shape (source aws.dms, detail-type "DMS Replication Task State Change",
# detail.category, detail.eventType, e.g. REPLICATION_TASK_STARTED) and the event IDs
# DMS-EVENT-0079 (Replication task stopped, StateChange) and DMS-EVENT-0078 (A replication task
# has failed, Failure). It does not show a stopped or failed sample event, so the exact
# detail.eventType value for a stop and the field that carries the event ID are not in the
# docs. The pattern therefore matches any one of: the Failure category, the eventType names
# that follow the documented REPLICATION_TASK_<STATE> form, or the event IDs. A branch whose
# field is absent simply never matches. The first live stop (make cdc-stall-drill) must show
# this rule matched (AWS/Events MatchedEvents for the rule); until then the state-change rule
# above still reports every stop.
resource "aws_cloudwatch_event_rule" "dms_task_stopped" {
  count = local.monitor ? 1 : 0

  name        = "${local.name}-dms-task-stopped"
  description = "ALARM: the dev CDC replication task stopped or failed; Silver and Gold go stale until it is resumed."
  event_pattern = jsonencode({
    source        = ["aws.dms"]
    "detail-type" = ["DMS Replication Task State Change"]
    resources     = [module.dms[0].replication_task_arn]
    detail = {
      "$or" = [
        { category = ["Failure"] },
        { eventType = ["REPLICATION_TASK_STOPPED", "REPLICATION_TASK_FAILED"] },
        { eventId = ["DMS-EVENT-0079", "DMS-EVENT-0078"] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "dms_task_stopped" {
  count = local.monitor ? 1 : 0

  rule = aws_cloudwatch_event_rule.dms_task_stopped[0].name
  arn  = local.alerts_arn

  input_transformer {
    input_paths = {
      event   = "$.detail.eventType"
      message = "$.detail.detailMessage"
      time    = "$.time"
      task    = "$.resources[0]"
    }
    input_template = "\"ALARM ProcureLake dev CDC: the DMS replication task stopped or failed at <time> (<event>: <message>; task <task>). Changes are not reaching Bronze, Silver or Gold. Runbook: docs/runbooks/cdc-dms-stall.md\""
  }
}
