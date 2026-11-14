# ----------------------------------------------------------------------------- document intelligence
# ADR 0045 (owner decision 2026-11-14, open question 41): when a contract document's landing
# manifest is written to the artifacts bucket, EventBridge starts one execution of the
# document_intelligence machine. It runs the extraction (Textract + Bedrock, ADR 0032) in the
# task runner with the AI extraction role as task role, queues the result for human review and
# notifies the reviewer. Approval, Silver and Gold stay human-driven (make ai-decide, then dbt).
#
# The artifacts bucket sends its object events to EventBridge (dev/foundation,
# eventbridge_buckets). Like the other runner machines, nothing here exists until the task
# runner image is deployed.

locals {
  di_machine = "${local.name_prefix}-document-intelligence"
  # Landing manifests of contract documents (procurelake.ai.documents, ingestion manifests).
  di_manifest_prefix = "${local.ai_manifests}contract_documents/"
}

resource "aws_iam_role" "document_intelligence" {
  name               = local.di_machine
  assume_role_policy = local.sfn_trust
}

resource "aws_iam_role_policy" "document_intelligence" {
  name = "run-extraction-task"
  role = aws_iam_role.document_intelligence.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "RunTask", Effect = "Allow", Action = "ecs:RunTask", Resource = module.task_runner.task_definition_arn },
      {
        Sid      = "TrackTask"
        Effect   = "Allow"
        Action   = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = "${replace(module.task_runner.cluster_arn, ":cluster/", ":task/")}/*"
      },
      { Sid = "SyncRule", Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = local.ecs_sync_rule },
      {
        # Extraction runs with the AI extraction role as task role (override).
        Sid       = "PassTaskRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [module.task_runner.execution_role_arn, aws_iam_role.ai_extraction.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ], local.alert_statements)
  })
}

resource "aws_sfn_state_machine" "document_intelligence" {
  count = local.runner_ready ? 1 : 0

  name     = local.di_machine
  role_arn = aws_iam_role.document_intelligence.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/document_intelligence.asl.json", {
    runner_cluster_arn         = module.task_runner.cluster_arn
    runner_task_definition_arn = module.task_runner.task_definition_arn
    runner_security_group_id   = module.task_runner.security_group_id
    runner_name                = local.runner_name
    subnet_a                   = module.orchestration_network.subnet_ids[0]
    subnet_b                   = module.orchestration_network.subnet_ids[1]
    ai_extraction_role_arn     = aws_iam_role.ai_extraction.arn
    alerts_topic_arn           = aws_sns_topic.pipeline_alerts.arn
  })
  depends_on = [aws_iam_role_policy.document_intelligence]
}

# Trigger: a document landing manifest written to the artifacts bucket.
resource "aws_cloudwatch_event_rule" "document_landed" {
  count = local.runner_ready && var.document_intelligence_on_landing ? 1 : 0

  name        = "${local.name_prefix}-document-landed"
  description = "A contract document's landing manifest was written: extract it (ADR 0045)."
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [local.buckets["artifacts"]] }
      object = { key = [{ prefix = local.di_manifest_prefix }] }
    }
  })
}

resource "aws_iam_role" "document_landed_events" {
  count = local.runner_ready && var.document_intelligence_on_landing ? 1 : 0

  name = "${local.name_prefix}-document-landed-events"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "document_landed_events" {
  count = local.runner_ready && var.document_intelligence_on_landing ? 1 : 0

  name = "start-document-intelligence"
  role = aws_iam_role.document_landed_events[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.document_intelligence[0].arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "document_landed" {
  count = local.runner_ready && var.document_intelligence_on_landing ? 1 : 0

  rule     = aws_cloudwatch_event_rule.document_landed[0].name
  arn      = aws_sfn_state_machine.document_intelligence[0].arn
  role_arn = aws_iam_role.document_landed_events[0].arn
}
