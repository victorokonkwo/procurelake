# ----------------------------------------------------------------------------- batch ingestion + data quality
# ADR 0040 (owner decisions 2026-11-12): batch ingestion and the daily data-quality checks run
# in a task runner container on ECS Fargate, started by Step Functions on EventBridge Scheduler
# schedules. The runner is the dbt runner module with its own image, repository, cluster, log
# group and security group. Its default task role is the ingestion role; the data-quality
# machine overrides it with the data engineer for the Silver checks.
#
# Nothing here exists until the task runner image is pushed (make task-runner-image) and its
# digest is given in task_runner_image_digest, and the dbt runner is deployed too.

locals {
  runner_ready      = var.task_runner_image_digest != "" && local.orchestrated
  runner_name       = "${local.name_prefix}-task-runner"
  ingestion_machine = "${local.name_prefix}-batch-ingestion"
  quality_machine   = "${local.name_prefix}-data-quality"
  ecs_sync_rule     = "${local.events_rule_arn}/StepFunctionsGetEventsForECSTaskRule"
  alert_statements = [
    { Sid = "Alert", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.pipeline_alerts.arn },
    { Sid = "EncryptAlert", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = local.foundation.data_kms_key_arn },
  ]
}

module "task_runner" {
  source = "../../../modules/dbt_runner"

  name           = local.runner_name
  container_name = "task"
  aws_account_id = var.aws_account_id
  region         = var.region
  vpc_id         = module.orchestration_network.vpc_id
  kms_key_arn    = local.foundation.data_kms_key_arn
  task_role_arn  = local.foundation.ingestion_role_arn
  image_digest   = var.task_runner_image_digest
  # The Canada contracts CSV (~655 MB) is streamed to ephemeral storage, parsed, then uploaded.
  cpu    = 1024
  memory = 2048

  environment = {
    AWS_REGION                       = var.region
    PROCURELAKE_RAW_BUCKET           = local.buckets["raw"]
    PROCURELAKE_ARTIFACTS_BUCKET     = local.buckets["artifacts"]
    PROCURELAKE_ATHENA_WORKGROUP     = local.foundation.athena_workgroup_name
    PROCURELAKE_SILVER_DATABASE      = local.silver_db
    PROCURELAKE_USASPENDING_SINCE    = var.usaspending_since
    PROCURELAKE_USASPENDING_MAX_DAYS = tostring(var.usaspending_max_days)
    # Weekly re-sweep (ADR 0044).
    PROCURELAKE_USASPENDING_RESWEEP_DAYS = tostring(var.usaspending_resweep_days)
  }
}

# batch_ingestion: one allow-listed ingestion task per execution.
resource "aws_iam_role" "batch_ingestion" {
  name               = local.ingestion_machine
  assume_role_policy = local.sfn_trust
}

resource "aws_iam_role_policy" "batch_ingestion" {
  count = local.runner_ready ? 1 : 0

  name = "run-ingestion-task"
  role = aws_iam_role.batch_ingestion.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "RunTask", Effect = "Allow", Action = "ecs:RunTask", Resource = module.task_runner.task_definition_arn },
      { Sid = "TrackTask", Effect = "Allow", Action = ["ecs:StopTask", "ecs:DescribeTasks"], Resource = "${replace(module.task_runner.cluster_arn, ":cluster/", ":task/")}/*" },
      { Sid = "SyncRule", Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = local.ecs_sync_rule },
      {
        Sid       = "PassTaskRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [module.task_runner.execution_role_arn, local.foundation.ingestion_role_arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      {
        # After a USAspending ingestion or re-sweep: apply the pending batches to Silver (ADR 0044).
        Sid      = "RunReferenceToSilver"
        Effect   = "Allow"
        Action   = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
        Resource = "${local.glue_prefix}:job/${module.reference_to_silver.job_name}"
      },
    ], local.alert_statements)
  })
}

resource "aws_sfn_state_machine" "batch_ingestion" {
  count = local.runner_ready ? 1 : 0

  name     = local.ingestion_machine
  role_arn = aws_iam_role.batch_ingestion.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/batch_ingestion.asl.json", {
    cluster_arn                  = module.task_runner.cluster_arn
    task_definition_arn          = module.task_runner.task_definition_arn
    subnet_a                     = module.orchestration_network.subnet_ids[0]
    subnet_b                     = module.orchestration_network.subnet_ids[1]
    security_group_id            = module.task_runner.security_group_id
    alerts_topic_arn             = aws_sns_topic.pipeline_alerts.arn
    runner_name                  = local.runner_name
    reference_to_silver_job_name = module.reference_to_silver.job_name
  })
  depends_on = [aws_iam_role_policy.batch_ingestion]
}

# data_quality: dbt test in the dbt runner, then the Silver checks as the data engineer.
resource "aws_iam_role" "data_quality" {
  name               = local.quality_machine
  assume_role_policy = local.sfn_trust
}

resource "aws_iam_role_policy" "data_quality" {
  count = local.runner_ready ? 1 : 0

  name = "run-quality-tasks"
  role = aws_iam_role.data_quality.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "RunTasks", Effect = "Allow", Action = "ecs:RunTask", Resource = [module.dbt_runner.task_definition_arn, module.task_runner.task_definition_arn] },
      {
        Sid    = "TrackTasks"
        Effect = "Allow"
        Action = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = [
          "${replace(module.dbt_runner.cluster_arn, ":cluster/", ":task/")}/*",
          "${replace(module.task_runner.cluster_arn, ":cluster/", ":task/")}/*",
        ]
      },
      { Sid = "SyncRule", Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = local.ecs_sync_rule },
      {
        # The Silver checks run in the task runner with the data engineer as task role (override).
        Sid       = "PassTaskRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [module.dbt_runner.execution_role_arn, module.task_runner.execution_role_arn, local.foundation.data_engineer_role_arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ], local.alert_statements)
  })
}

resource "aws_sfn_state_machine" "data_quality" {
  count = local.runner_ready ? 1 : 0

  name     = local.quality_machine
  role_arn = aws_iam_role.data_quality.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/data_quality.asl.json", {
    dbt_cluster_arn            = module.dbt_runner.cluster_arn
    dbt_task_definition_arn    = module.dbt_runner.task_definition_arn
    dbt_security_group_id      = module.dbt_runner.security_group_id
    runner_cluster_arn         = module.task_runner.cluster_arn
    runner_task_definition_arn = module.task_runner.task_definition_arn
    runner_security_group_id   = module.task_runner.security_group_id
    subnet_a                   = module.orchestration_network.subnet_ids[0]
    subnet_b                   = module.orchestration_network.subnet_ids[1]
    data_engineer_role_arn     = local.foundation.data_engineer_role_arn
    alerts_topic_arn           = aws_sns_topic.pipeline_alerts.arn
  })
  depends_on = [aws_iam_role_policy.data_quality]
}

# Schedules (America/Toronto). Each can be turned off with its variable.
resource "aws_iam_role" "batch_scheduler" {
  count = local.runner_ready ? 1 : 0

  name = "${local.name_prefix}-batch-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "batch_scheduler" {
  count = local.runner_ready ? 1 : 0

  name = "start-batch-machines"
  role = aws_iam_role.batch_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = [aws_sfn_state_machine.batch_ingestion[0].arn, aws_sfn_state_machine.data_quality[0].arn]
    }]
  })
}

locals {
  batch_schedules = local.runner_ready ? {
    usaspending = {
      enabled     = var.schedule_usaspending != ""
      expression  = var.schedule_usaspending
      machine     = aws_sfn_state_machine.batch_ingestion[0].arn
      input       = { task = "ingest-usaspending" }
      description = "Fill missing settled USAspending days (ADR 0020, 0040)."
    }
    usaspending-resweep = {
      enabled     = var.schedule_usaspending_resweep != ""
      expression  = var.schedule_usaspending_resweep
      machine     = aws_sfn_state_machine.batch_ingestion[0].arn
      input       = { task = "resweep-usaspending" }
      description = "Weekly: collect the most recent settled USAspending days again, then apply to Silver (ADR 0044)."
    }
    canada-contracts = {
      enabled     = var.schedule_canada_contracts != ""
      expression  = var.schedule_canada_contracts
      machine     = aws_sfn_state_machine.batch_ingestion[0].arn
      input       = { task = "ingest-canada-contracts" }
      description = "Land the Government of Canada contracts CSV when its ETag changes (ADR 0039, 0040)."
    }
    data-quality = {
      enabled     = var.schedule_data_quality != ""
      expression  = var.schedule_data_quality
      machine     = aws_sfn_state_machine.data_quality[0].arn
      input       = {}
      description = "Daily dbt test plus Bronze-to-Silver reconciliation and Silver integrity (ADR 0040)."
    }
  } : {}
}

resource "aws_scheduler_schedule" "batch" {
  for_each = { for k, v in local.batch_schedules : k => v if v.enabled }

  name                         = "${local.name_prefix}-${each.key}"
  description                  = each.value.description
  schedule_expression          = each.value.expression
  schedule_expression_timezone = "America/Toronto"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = each.value.machine
    role_arn = aws_iam_role.batch_scheduler[0].arn
    input    = jsonencode(each.value.input)
  }

  depends_on = [aws_iam_role_policy.batch_scheduler]
}
