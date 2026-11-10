# dev / data_platform: Glue jobs that build Silver (ADR 0025, ADR 0028) and the CDC checkpoint
# table. Storage, keys, catalog databases and workload roles come from dev/foundation, read
# from its remote state.

locals {
  name_prefix = "procurelake-dev"

  foundation  = data.terraform_remote_state.foundation.outputs
  buckets     = local.foundation.bucket_names
  raw_arn     = "arn:aws:s3:::${local.buckets["raw"]}"
  lake_arn    = "arn:aws:s3:::${local.buckets["lake"]}"
  art_arn     = "arn:aws:s3:::${local.buckets["artifacts"]}"
  silver_db   = local.foundation.silver_database_name
  glue_prefix = "arn:aws:glue:${var.region}:${var.aws_account_id}"

  # Iceberg on Glue 5.1 (AWS Glue docs, "Using the Iceberg framework in AWS Glue"): the bundled
  # Iceberg 1.10.0 runtime, with a Spark catalog backed by the Glue Data Catalog.
  iceberg_conf = join(" --conf ", [
    "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    "spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog",
    "spark.sql.catalog.glue_catalog.warehouse=s3://${local.buckets["lake"]}/silver/",
    "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog",
    "spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
    "spark.sql.session.timeZone=UTC",
  ])

  wheel_path = "${var.dist_dir}/${one(fileset(var.dist_dir, "procurelake-*-py3-none-any.whl"))}"
  python_modules = [
    for line in split("\n", file("${path.module}/../../../../jobs/glue/silver_python_modules.txt")) :
    trimspace(line) if trimspace(line) != "" && !startswith(trimspace(line), "#")
  ]
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "dev/foundation/terraform.tfstate"
    region = var.region
  }
}

# ----------------------------------------------------------------------------- bronze -> silver
# Data access for the Silver job: read PostgreSQL snapshots in Bronze and their manifests,
# write Silver Iceberg tables, Silver quarantine and run summaries. Bronze deletes are denied.
locals {
  bronze_to_silver_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListJobPrefixesRaw"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.raw_arn
        Condition = { StringLike = { "s3:prefix" = ["bronze/postgres/*", "quarantine/silver/*"] } }
      },
      {
        Sid       = "ListJobPrefixesLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["silver/*"] } }
      },
      {
        Sid       = "ListJobPrefixesArtifacts"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.art_arn
        Condition = { StringLike = { "s3:prefix" = ["pipeline_manifests/postgres/*", "pipeline_runs/silver/*"] } }
      },
      {
        Sid      = "ReadPostgresSnapshots"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${local.raw_arn}/bronze/postgres/*", "${local.art_arn}/pipeline_manifests/postgres/*"]
      },
      {
        Sid      = "WriteQuarantineAndSilver"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.raw_arn}/quarantine/silver/*", "${local.lake_arn}/silver/*"]
      },
      {
        Sid      = "WriteRunSummaries"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${local.art_arn}/pipeline_runs/silver/*"
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "ManageSilverIcebergTables"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.silver_db}",
          "${local.glue_prefix}:table/${local.silver_db}/*",
        ]
      },
      {
        # Bronze is immutable (ADR 0002). Silver quarantine is the only raw prefix it may delete
        # in, because Spark's file committer renames (copy + delete) its output there.
        Sid      = "DenyBronzeDeletes"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = ["${local.raw_arn}/bronze/*", "${local.raw_arn}/documents/*"]
      },
    ]
  })
}

module "bronze_to_silver" {
  source = "../../../modules/glue_spark_job"

  name                    = "${local.name_prefix}-bronze-to-silver"
  description             = "Apply the latest complete PostgreSQL snapshot batch from Bronze to Silver Iceberg tables."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/bronze_to_silver.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.bronze_to_silver_policy

  log_group_arns = [for g in aws_cloudwatch_log_group.glue : g.arn]

  job_arguments = {
    "--datalake-formats" = "iceberg"
    "--conf"             = local.iceberg_conf
    "--artifacts-bucket" = local.buckets["artifacts"]
    "--catalog"          = "glue_catalog"
    "--silver-database"  = local.silver_db
    "--silver-location"  = "s3://${local.buckets["lake"]}/silver"
    "--quarantine-root"  = "s3://${local.buckets["raw"]}/quarantine"
    "--region"           = var.region
  }
}

# ----------------------------------------------------------------------------- shared Glue logs
# Glue 5.x writes every job's driver/executor logs to these account-wide groups (AWS Glue docs,
# "Logging for AWS Glue jobs"); created here once so their retention is managed.
resource "aws_cloudwatch_log_group" "glue" {
  for_each = toset(["/aws-glue/jobs/error", "/aws-glue/jobs/output"])

  name              = each.value
  retention_in_days = 30
}

moved {
  from = module.bronze_to_silver.aws_cloudwatch_log_group.glue["/aws-glue/jobs/error"]
  to   = aws_cloudwatch_log_group.glue["/aws-glue/jobs/error"]
}

moved {
  from = module.bronze_to_silver.aws_cloudwatch_log_group.glue["/aws-glue/jobs/output"]
  to   = aws_cloudwatch_log_group.glue["/aws-glue/jobs/output"]
}

# ----------------------------------------------------------------------------- CDC checkpoints
# Leases, applied-file records and checkpoints for CDC apply (ADR 0028). Persistent (it must
# outlive the disposable CDC stack); pay-per-request, encrypted with the data key.
resource "aws_dynamodb_table" "cdc_checkpoints" {
  name                        = "${local.name_prefix}-cdc-checkpoints"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.foundation.data_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ----------------------------------------------------------------------------- CDC apply job
# Data access for the CDC job: read DMS output in Bronze, write Silver and its quarantine, use
# the checkpoint table, and read DMS task/endpoint/table status (full-load completeness).
locals {
  apply_cdc_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListCdcAndQuarantineRaw"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.raw_arn
        Condition = { StringLike = { "s3:prefix" = ["bronze/postgres_cdc/*", "quarantine/silver_cdc/*"] } }
      },
      {
        Sid       = "ListSilverLake"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = local.lake_arn
        Condition = { StringLike = { "s3:prefix" = ["silver/*"] } }
      },
      {
        Sid      = "ReadCdcFiles"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${local.raw_arn}/bronze/postgres_cdc/*"
      },
      {
        Sid      = "WriteSilverAndQuarantine"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.raw_arn}/quarantine/silver_cdc/*", "${local.lake_arn}/silver/*"]
      },
      {
        Sid      = "UseDataKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = local.foundation.data_kms_key_arn
      },
      {
        Sid    = "ManageSilverIcebergTables"
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable"]
        Resource = [
          "${local.glue_prefix}:catalog",
          "${local.glue_prefix}:database/${local.silver_db}",
          "${local.glue_prefix}:table/${local.silver_db}/*",
        ]
      },
      {
        # TransactWriteItems is authorized per item as PutItem / ConditionCheckItem.
        Sid      = "UseCheckpointTable"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:ConditionCheckItem"]
        Resource = aws_dynamodb_table.cdc_checkpoints.arn
      },
      {
        # DMS describe calls do not support resource-level restriction for these filters.
        Sid      = "ReadDmsTaskStatus"
        Effect   = "Allow"
        Action   = ["dms:DescribeReplicationTasks", "dms:DescribeEndpoints", "dms:DescribeTableStatistics"]
        Resource = "*"
      },
      {
        # CDC freshness and quality metrics (src/procurelake/cdc/metrics.py). PutMetricData has
        # no resource-level permissions; the namespace condition limits it.
        Sid       = "PublishCdcMetrics"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "ProcureLake/CDC" } }
      },
      {
        Sid      = "DenyBronzeDeletes"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource = ["${local.raw_arn}/bronze/*", "${local.raw_arn}/documents/*"]
      },
    ]
  })
}

module "apply_cdc" {
  source = "../../../modules/glue_spark_job"

  name                    = "${local.name_prefix}-apply-cdc"
  description             = "Apply new AWS DMS change files from Bronze to Silver Iceberg (lease + checkpoint in DynamoDB)."
  aws_account_id          = var.aws_account_id
  script_path             = "${path.module}/../../../../jobs/glue/apply_cdc.py"
  wheel_path              = local.wheel_path
  python_modules          = local.python_modules
  artifacts_bucket_name   = local.buckets["artifacts"]
  kms_key_arn             = local.foundation.data_kms_key_arn
  data_access_policy_json = local.apply_cdc_policy
  log_group_arns          = [for g in aws_cloudwatch_log_group.glue : g.arn]

  job_arguments = {
    "--datalake-formats" = "iceberg"
    "--conf"             = local.iceberg_conf
    "--raw-bucket"       = local.buckets["raw"]
    "--cdc-root"         = "bronze/postgres_cdc"
    "--checkpoint-table" = aws_dynamodb_table.cdc_checkpoints.name
    "--catalog"          = "glue_catalog"
    "--silver-database"  = local.silver_db
    "--silver-location"  = "s3://${local.buckets["lake"]}/silver"
    "--quarantine-root"  = "s3://${local.buckets["raw"]}/quarantine"
    "--region"           = var.region
    "--environment"      = "dev" # publish ProcureLake/CDC metrics (Week 8)
  }
}

# ----------------------------------------------------------------------------- orchestration
# Step Functions for CDC processing and dbt transformation (ADR 0006, ADR 0029). The schedule
# that starts cdc_processing lives in dev/cdc, so it exists only while CDC is up.
module "orchestration_network" {
  source = "../../../modules/public_network"

  name = "${local.name_prefix}-orchestration"
}

module "dbt_runner" {
  source = "../../../modules/dbt_runner"

  name           = "${local.name_prefix}-dbt-runner"
  aws_account_id = var.aws_account_id
  region         = var.region
  vpc_id         = module.orchestration_network.vpc_id
  kms_key_arn    = local.foundation.data_kms_key_arn
  task_role_arn  = local.foundation.data_engineer_role_arn
  image_digest   = var.dbt_image_digest

  environment = {
    AWS_REGION                     = var.region
    PROCURELAKE_DBT_S3_STAGING_DIR = "s3://${local.buckets["artifacts"]}/athena_results/"
    PROCURELAKE_DBT_S3_DATA_DIR    = "s3://${local.buckets["lake"]}/gold/"
    PROCURELAKE_GOLD_SCHEMA        = local.foundation.gold_database_name
    PROCURELAKE_SILVER_SCHEMA      = local.silver_db
    PROCURELAKE_ATHENA_WORKGROUP   = local.foundation.athena_workgroup_name
    PROCURELAKE_ARTIFACTS_BUCKET   = local.buckets["artifacts"]
  }
}

resource "aws_sns_topic" "pipeline_alerts" {
  name              = "${local.name_prefix}-pipeline-alerts"
  kms_master_key_id = local.foundation.data_kms_key_arn
}

# The default topic policy (owner account), plus CloudWatch alarms and EventBridge rules of
# this account (Week 8 monitoring). The KMS key policy lets both use the key through SNS.
resource "aws_sns_topic_policy" "pipeline_alerts" {
  arn = aws_sns_topic.pipeline_alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "OwnerAccount"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action = [
          "SNS:GetTopicAttributes", "SNS:SetTopicAttributes", "SNS:AddPermission", "SNS:RemovePermission",
          "SNS:DeleteTopic", "SNS:Subscribe", "SNS:ListSubscriptionsByTopic", "SNS:Publish",
        ]
        Resource  = aws_sns_topic.pipeline_alerts.arn
        Condition = { StringEquals = { "AWS:SourceOwner" = var.aws_account_id } }
      },
      {
        Sid       = "CloudWatchAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.pipeline_alerts.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = var.aws_account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.region}:${var.aws_account_id}:alarm:${local.name_prefix}-*" }
        }
      },
      {
        Sid       = "EventBridgeRules"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.pipeline_alerts.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = var.aws_account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:events:${var.region}:${var.aws_account_id}:rule/${local.name_prefix}-*" }
        }
      },
    ]
  })
}

resource "aws_sns_topic_subscription" "pipeline_alerts_email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.pipeline_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  sfn_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
  sfn_prefix       = "arn:aws:states:${var.region}:${var.aws_account_id}"
  dbt_machine      = "${local.name_prefix}-dbt-transformation"
  access_check     = "${local.name_prefix}-tenant-access-check"
  access_check_arn = "${local.sfn_prefix}:stateMachine:${local.access_check}"
  cdc_machine      = "${local.name_prefix}-cdc-processing"
  orchestrated     = var.dbt_image_digest != ""
  events_rule_arn  = "arn:aws:events:${var.region}:${var.aws_account_id}:rule"
}

# dbt_transformation: run the dbt runner task and check its exit code.
resource "aws_iam_role" "dbt_transformation" {
  name               = local.dbt_machine
  assume_role_policy = local.sfn_trust
}

resource "aws_iam_role_policy" "dbt_transformation" {
  count = local.orchestrated ? 1 : 0

  name = "run-dbt-task"
  role = aws_iam_role.dbt_transformation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Step Functions docs, "Run a Job (.sync)" policy for Amazon ECS.
      { Sid = "RunDbtTask", Effect = "Allow", Action = "ecs:RunTask", Resource = module.dbt_runner.task_definition_arn },
      { Sid = "TrackTask", Effect = "Allow", Action = ["ecs:StopTask", "ecs:DescribeTasks"], Resource = "*" },
      {
        Sid      = "SyncRule"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = "${local.events_rule_arn}/StepFunctionsGetEventsForECSTaskRule"
      },
      {
        Sid       = "PassTaskRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [module.dbt_runner.execution_role_arn, local.foundation.data_engineer_role_arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      { Sid = "StartAccessCheck", Effect = "Allow", Action = "states:StartExecution", Resource = local.access_check_arn },
      {
        Sid      = "TrackAccessCheck"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution", "states:StopExecution"]
        Resource = "${local.sfn_prefix}:execution:${local.access_check}:*"
      },
      {
        Sid      = "SyncRuleNested"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = "${local.events_rule_arn}/StepFunctionsGetEventsForStepFunctionsExecutionRule"
      },
    ]
  })
}

resource "aws_sfn_state_machine" "dbt_transformation" {
  count = local.orchestrated ? 1 : 0

  name     = local.dbt_machine
  role_arn = aws_iam_role.dbt_transformation.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/dbt_transformation.asl.json", {
    cluster_arn         = module.dbt_runner.cluster_arn
    task_definition_arn = module.dbt_runner.task_definition_arn
    subnet_a            = module.orchestration_network.subnet_ids[0]
    subnet_b            = module.orchestration_network.subnet_ids[1]
    security_group_id   = module.dbt_runner.security_group_id
    # Created in dev/security, referenced by its fixed name (no remote-state cycle).
    tenant_access_check_arn = local.access_check_arn
  })
  depends_on = [aws_iam_role_policy.dbt_transformation]
}

# cdc_processing: skip if another execution runs, CDC apply (Glue), then dbt_transformation.
resource "aws_iam_role" "cdc_processing" {
  name               = local.cdc_machine
  assume_role_policy = local.sfn_trust
}

resource "aws_iam_role_policy" "cdc_processing" {
  count = local.orchestrated ? 1 : 0

  name = "cdc-processing"
  role = aws_iam_role.cdc_processing.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "CountOwnExecutions", Effect = "Allow", Action = "states:ListExecutions", Resource = "${local.sfn_prefix}:stateMachine:${local.cdc_machine}" },
      {
        Sid      = "RunCdcApply"
        Effect   = "Allow"
        Action   = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
        Resource = "arn:aws:glue:${var.region}:${var.aws_account_id}:job/${module.apply_cdc.job_name}"
      },
      { Sid = "StartDbt", Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.dbt_transformation[0].arn },
      {
        Sid      = "TrackDbt"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution", "states:StopExecution"]
        Resource = "${local.sfn_prefix}:execution:${local.dbt_machine}:*"
      },
      {
        Sid      = "SyncRule"
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = "${local.events_rule_arn}/StepFunctionsGetEventsForStepFunctionsExecutionRule"
      },
      { Sid = "Alert", Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.pipeline_alerts.arn },
      { Sid = "EncryptAlert", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = local.foundation.data_kms_key_arn },
    ]
  })
}

resource "aws_sfn_state_machine" "cdc_processing" {
  count = local.orchestrated ? 1 : 0

  name     = local.cdc_machine
  role_arn = aws_iam_role.cdc_processing.arn
  definition = templatefile("${path.module}/../../../../orchestration/step_functions/cdc_processing.asl.json", {
    apply_cdc_job_name    = module.apply_cdc.job_name
    dbt_state_machine_arn = aws_sfn_state_machine.dbt_transformation[0].arn
    alerts_topic_arn      = aws_sns_topic.pipeline_alerts.arn
  })
  depends_on = [aws_iam_role_policy.cdc_processing]
}
