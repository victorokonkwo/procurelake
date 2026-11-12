# dev / observability: alarms and the operations dashboard (implementation plan E, Week 8).
#
# Alarms publish to the pipeline alerts topic (dev/data_platform); the topic's KMS key and
# policy allow CloudWatch (foundation, data_platform).
#
#   - Every state machine: any failed, timed-out or aborted execution. The CDC and export
#     machines also alert from inside the workflow; the alarm catches what they cannot report
#     themselves (an alert step that failed, a timeout, an abort).
#   - CDC freshness: a batch reached Silver more than 15 minutes after its oldest source commit.
#   - CDC quality: events rejected to quarantine, keys held, or a batch blocked by the gate.
#
# DMS alarms (replication lag, task stopped) live in dev/cdc, so they exist only with the task.

locals {
  name       = "procurelake-dev"
  dp         = data.terraform_remote_state.data_platform.outputs
  sec        = data.terraform_remote_state.security.outputs
  alerts_arn = local.dp.pipeline_alerts_topic_arn
  machines = merge({
    cdc-processing      = local.dp.cdc_processing_state_machine_arn
    dbt-transformation  = local.dp.dbt_transformation_state_machine_arn
    tenant-exports      = local.sec.tenant_exports_state_machine_arn
    tenant-access-check = local.sec.tenant_access_check_state_machine_arn
    }, {
    # ADR 0040: null until the task runner image is deployed; try() covers state written before
    # these outputs existed.
    for k, v in {
      batch-ingestion = try(local.dp.batch_ingestion_state_machine_arn, null)
      data-quality    = try(local.dp.data_quality_state_machine_arn, null)
    } : k => v if v != null
  })
  cdc_namespace = "ProcureLake/CDC"
}

data "terraform_remote_state" "data_platform" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/data_platform/terraform.tfstate"
    region = var.region
  }
}

data "terraform_remote_state" "security" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/security/terraform.tfstate"
    region = var.region
  }
}

# ----------------------------------------------------------------------------- state machines
resource "aws_cloudwatch_metric_alarm" "state_machine_failed" {
  for_each = local.machines

  alarm_name          = "${local.name}-sfn-${each.key}-failed"
  alarm_description   = "An execution of ${each.key} failed, timed out or was aborted. Runbook: docs/runbooks/pipeline-alarms.md"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching" # no executions in the period is normal
  alarm_actions       = [local.alerts_arn]
  ok_actions          = [local.alerts_arn]

  metric_query {
    id          = "bad"
    expression  = "failed + timedout + aborted"
    label       = "Failed, timed out or aborted executions"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { failed = "ExecutionsFailed", timedout = "ExecutionsTimedOut", aborted = "ExecutionsAborted" }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/States"
        metric_name = metric_query.value
        dimensions  = { StateMachineArn = each.value }
        period      = 300
        stat        = "Sum"
      }
    }
  }
}

# ----------------------------------------------------------------------------- CDC (custom metrics)
# Metrics Insights queries aggregate across every Silver table (dimension Table).
resource "aws_cloudwatch_metric_alarm" "cdc_freshness" {
  alarm_name          = "${local.name}-cdc-source-to-silver-latency"
  alarm_description   = "A CDC batch reached Silver more than ${var.freshness_threshold_seconds} s after its oldest source commit (C5 gate: 15 min to Gold). Runbook: docs/runbooks/pipeline-alarms.md, row cdc-source-to-silver-latency"
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.freshness_threshold_seconds
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching" # no batch = nothing applied (quiet source or DMS stall: see dev/cdc alarms)
  alarm_actions       = [local.alerts_arn]
  ok_actions          = [local.alerts_arn]

  metric_query {
    id          = "latency"
    expression  = "SELECT MAX(SourceToSilverSeconds) FROM SCHEMA(\"${local.cdc_namespace}\", Environment, \"Table\") WHERE Environment = 'dev'"
    period      = 300
    label       = "Max source-to-Silver latency (s)"
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "cdc_quality" {
  for_each = {
    rejected = { metric = "RejectedEvents", what = "CDC events were rejected to quarantine" }
    held     = { metric = "HeldKeys", what = "keys were held at their previous version (latest event rejected)" }
    blocked  = { metric = "BlockedBatches", what = "a CDC batch was blocked by the quality gate" }
  }

  alarm_name          = "${local.name}-cdc-${each.key}"
  alarm_description   = "${title(each.value.what)}. Runbook: docs/runbooks/pipeline-alarms.md"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alerts_arn]
  ok_actions          = [local.alerts_arn]

  metric_query {
    id          = "q"
    expression  = "SELECT SUM(${each.value.metric}) FROM SCHEMA(\"${local.cdc_namespace}\", Environment, \"Table\") WHERE Environment = 'dev'"
    period      = 300
    label       = each.value.metric
    return_data = true
  }
}

# ----------------------------------------------------------------------------- dashboard
resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "${local.name}-operations"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "alarm", x = 0, y = 0, width = 24, height = 3
        properties = {
          title = "Alarms"
          alarms = concat(
            [for a in aws_cloudwatch_metric_alarm.state_machine_failed : a.arn],
            [aws_cloudwatch_metric_alarm.cdc_freshness.arn],
            [for a in aws_cloudwatch_metric_alarm.cdc_quality : a.arn],
          )
        }
      },
      {
        type = "metric", x = 0, y = 3, width = 12, height = 6
        properties = {
          title  = "Step Functions: succeeded vs failed (5 min)"
          region = var.region
          stat   = "Sum"
          period = 300
          # One row per metric (concat, not flatten: flatten would also flatten each row).
          metrics = concat(
            [for k, arn in local.machines : ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", arn, { label = "${k} ok" }]],
            [for k, arn in local.machines : ["AWS/States", "ExecutionsFailed", "StateMachineArn", arn, { label = "${k} failed" }]],
          )
        }
      },
      {
        type = "metric", x = 12, y = 3, width = 12, height = 6
        properties = {
          title  = "CDC: source commit to Silver (s), max per table"
          region = var.region
          period = 300
          metrics = [[{
            expression = "SEARCH('{${local.cdc_namespace},Environment,Table} MetricName=\"SourceToSilverSeconds\" Environment=\"dev\"', 'Maximum', 300)"
            id         = "lat"
          }]]
          annotations = { horizontal = [{ label = "15 min gate", value = var.freshness_threshold_seconds }] }
        }
      },
      {
        type = "metric", x = 0, y = 9, width = 12, height = 6
        properties = {
          title  = "CDC: events applied and rejected per table"
          region = var.region
          period = 300
          metrics = [
            [{ expression = "SEARCH('{${local.cdc_namespace},Environment,Table} MetricName=\"AppliedEvents\" Environment=\"dev\"', 'Sum', 300)", id = "applied" }],
            [{ expression = "SEARCH('{${local.cdc_namespace},Environment,Table} MetricName=\"RejectedEvents\" Environment=\"dev\"', 'Sum', 300)", id = "rejected" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 9, width = 12, height = 6
        properties = {
          title  = "DMS: CDC latency source / target (s); present only while dev/cdc is up"
          region = var.region
          period = 60
          metrics = [
            [{ expression = "SEARCH('{AWS/DMS,ReplicationInstanceIdentifier,ReplicationTaskIdentifier} MetricName=\"CDCLatencySource\"', 'Maximum', 60)", id = "src" }],
            [{ expression = "SEARCH('{AWS/DMS,ReplicationInstanceIdentifier,ReplicationTaskIdentifier} MetricName=\"CDCLatencyTarget\"', 'Maximum', 60)", id = "tgt" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 15, width = 24, height = 6
        properties = {
          title  = "Athena: bytes scanned per workgroup (cost)"
          region = var.region
          period = 3600
          metrics = [[{
            expression = "SEARCH('{AWS/Athena,QueryState,QueryType,WorkGroup} MetricName=\"ProcessedBytes\"', 'Sum', 3600)"
            id         = "bytes"
          }]]
        }
      },
    ]
  })
}
