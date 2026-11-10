mock_provider "aws" {}

override_data {
  target = data.terraform_remote_state.data_platform
  values = {
    outputs = {
      pipeline_alerts_topic_arn            = "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts"
      cdc_processing_state_machine_arn     = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-cdc-processing"
      dbt_transformation_state_machine_arn = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-dbt-transformation"
    }
  }
}

override_data {
  target = data.terraform_remote_state.security
  values = {
    outputs = {
      tenant_exports_state_machine_arn      = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-tenant-exports"
      tenant_access_check_state_machine_arn = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-tenant-access-check"
    }
  }
}

variables {
  aws_account_id = "123456789012"
  state_bucket   = "procurelake-tfstate-123456789012-ca-central-1"
}

run "every_state_machine_alarms_on_failed_timed_out_or_aborted" {
  command = apply

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.state_machine_failed) == 4
    error_message = "All four state machines need a failure alarm."
  }
  assert {
    condition = alltrue([for a in aws_cloudwatch_metric_alarm.state_machine_failed :
      toset([for q in a.metric_query : try(q.metric[0].metric_name, "")]) == toset(["", "ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"])
    && a.treat_missing_data == "notBreaching" && a.threshold == 1])
    error_message = "Failed + timed out + aborted, >= 1, no executions is not an alarm."
  }
}

run "alarms_notify_the_alerts_topic_and_name_the_runbook" {
  command = apply

  assert {
    condition = alltrue([for a in concat(values(aws_cloudwatch_metric_alarm.state_machine_failed), [aws_cloudwatch_metric_alarm.cdc_freshness], values(aws_cloudwatch_metric_alarm.cdc_quality)) :
    contains(a.alarm_actions, "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts") && strcontains(a.alarm_description, "docs/runbooks/") && startswith(a.alarm_name, "procurelake-dev-")])
    error_message = "Every alarm notifies the alerts topic, names its runbook and uses the prefix the topic policy allows."
  }
}

run "cdc_freshness_uses_the_15_minute_gate" {
  command = apply

  assert {
    condition     = aws_cloudwatch_metric_alarm.cdc_freshness.threshold == 900 && strcontains(one(aws_cloudwatch_metric_alarm.cdc_freshness.metric_query).expression, "MAX(SourceToSilverSeconds)")
    error_message = "Freshness alarms above 900 s on the worst table."
  }
}

run "dashboard_metric_rows_are_arrays" {
  command = apply

  # PutDashboard rejects a widget whose `metrics` is not an array of arrays (live, 2026-11-10).
  assert {
    condition = alltrue([for w in jsondecode(aws_cloudwatch_dashboard.operations.dashboard_body).widgets :
    w.type != "metric" || alltrue([for row in w.properties.metrics : can(length(row)) && !can(regex("", row))])])
    error_message = "Every metric widget row must be an array."
  }
}
