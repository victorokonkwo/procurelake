mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["ca-central-1a", "ca-central-1b"] }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address            = "src.example.ca-central-1.rds.amazonaws.com"
      port               = 5432
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:ca-central-1:123456789012:secret:rds!db-1", kms_key_id = "k", secret_status = "active" }]
    }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:ca-central-1:123456789012:secret:replication-user" }
  }
  mock_resource "aws_scheduler_schedule" {
    defaults = { arn = "arn:aws:scheduler:ca-central-1:123456789012:schedule/default/procurelake-dev-cdc-processing" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-cdc-x" }
  }
  mock_resource "aws_dms_replication_instance" {
    defaults = { replication_instance_arn = "arn:aws:dms:ca-central-1:123456789012:rep:MOCK" }
  }
  mock_resource "aws_dms_endpoint" {
    defaults = { endpoint_arn = "arn:aws:dms:ca-central-1:123456789012:endpoint:MOCKSRC" }
  }
  mock_resource "aws_dms_replication_task" {
    defaults = { replication_task_arn = "arn:aws:dms:ca-central-1:123456789012:task:MOCKTASKRESOURCEID" }
  }
  mock_resource "aws_dms_s3_endpoint" {
    defaults = { endpoint_arn = "arn:aws:dms:ca-central-1:123456789012:endpoint:MOCKTGT" }
  }
}

override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      bucket_names     = { raw = "procurelake-dev-raw-123456789012-ca-central-1" }
      data_kms_key_arn = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  }
}

override_data {
  target = data.terraform_remote_state.data_platform
  values = {
    outputs = {
      cdc_processing_state_machine_arn = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-cdc-processing"
      pipeline_alerts_topic_arn        = "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts"
    }
  }
}

variables {
  aws_account_id = "123456789012"
  state_bucket   = "procurelake-tfstate-123456789012-ca-central-1"
  operator_cidrs = ["203.0.113.7/32"]
}

run "phase_one_has_no_dms_alarms" {
  command = apply

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.dms_source_latency) == 0 && length(aws_cloudwatch_event_rule.dms_task_state) == 0
    error_message = "Without a DMS task there is nothing to monitor (and nothing must alarm)."
  }
}

run "phase_two_alarms_on_a_stalled_or_stopped_task" {
  command = apply

  variables {
    enable_dms = true
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.dms_source_latency[0].treat_missing_data == "breaching" && aws_cloudwatch_metric_alarm.dms_source_latency[0].threshold == 300
    error_message = "A stopped task publishes no metrics: missing data must alarm, like latency over 5 minutes."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.dms_source_latency[0].dimensions["ReplicationTaskIdentifier"] == "MOCKTASKRESOURCEID"
    error_message = "The task dimension is the task's resource identifier (last part of its ARN)."
  }
  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.dms_task_state[0].event_pattern).resources == ["arn:aws:dms:ca-central-1:123456789012:task:MOCKTASKRESOURCEID"] && aws_cloudwatch_event_target.dms_task_state[0].arn == "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts"
    error_message = "Task state changes of this task go to the alerts topic."
  }
}

run "phase_one_has_no_dms" {
  command = apply

  assert {
    condition     = length(module.dms) == 0 && output.replication_task_arn == null
    error_message = "DMS must not exist until the source is set up (enable_dms = true)."
  }
  assert {
    condition     = output.cdc_schedule_name == null
    error_message = "No schedule without DMS."
  }
}

run "phase_two_replicates_into_bronze" {
  command = apply

  variables {
    enable_dms = true
  }

  assert {
    condition     = startswith(output.bronze_cdc_prefix, "bronze/postgres_cdc/load_id=")
    error_message = "DMS output must land in Bronze under a load_id prefix."
  }
  assert {
    condition     = aws_scheduler_schedule.cdc_processing[0].schedule_expression == "rate(5 minutes)" && aws_scheduler_schedule.cdc_processing[0].flexible_time_window[0].mode == "OFF"
    error_message = "cdc_processing runs every 5 minutes while CDC is up."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.scheduler[0].policy).Statement[0].Resource == "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-cdc-processing"
    error_message = "The scheduler may start only cdc_processing."
  }
}
