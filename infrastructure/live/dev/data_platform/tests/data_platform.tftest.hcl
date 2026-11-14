mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-bronze-to-silver" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ca-central-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:ca-central-1:123456789012:repository/procurelake-dev-dbt-runner"
      repository_url = "123456789012.dkr.ecr.ca-central-1.amazonaws.com/procurelake-dev-dbt-runner"
    }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:ca-central-1:123456789012:task-definition/procurelake-dev-dbt-runner:1" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:ca-central-1:123456789012:cluster/procurelake-dev-dbt-runner" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-dbt-transformation" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ca-central-1:123456789012:procurelake-dev-pipeline-alerts" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["ca-central-1a", "ca-central-1b"] }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:ca-central-1:123456789012:table/procurelake-dev-cdc-checkpoints" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      bucket_names = {
        raw       = "procurelake-dev-raw-123456789012-ca-central-1"
        lake      = "procurelake-dev-lake-123456789012-ca-central-1"
        products  = "procurelake-dev-products-123456789012-ca-central-1"
        artifacts = "procurelake-dev-artifacts-123456789012-ca-central-1"
      }
      data_kms_key_arn           = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
      silver_database_name       = "procurelake_dev_silver"
      gold_database_name         = "procurelake_dev_gold"
      bench_silver_database_name = "procurelake_dev_bench_silver"
      bench_gold_database_name   = "procurelake_dev_bench_gold"
      athena_workgroup_name      = "procurelake-dev-engineering"
      data_engineer_role_arn     = "arn:aws:iam::123456789012:role/procurelake-dev-data-engineer"
      ingestion_role_arn         = "arn:aws:iam::123456789012:role/procurelake-dev-ingestion"
    }
  }
}

variables {
  operator_arns  = ["arn:aws:iam::123456789012:user/operator"]
  aws_account_id = "123456789012"
  state_bucket   = "procurelake-tfstate-123456789012-ca-central-1"
  dist_dir       = "../../../modules/glue_spark_job/tests/fixtures"
}

run "silver_job_is_wired_to_the_glue_iceberg_catalog" {
  command = apply

  assert {
    condition     = output.bronze_to_silver_job_name == "procurelake-dev-bronze-to-silver"
    error_message = "Unexpected job name."
  }
  assert {
    condition     = strcontains(module.bronze_to_silver.job_arguments_for_test["--conf"], "spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog")
    error_message = "The Iceberg catalog must be backed by the Glue Data Catalog."
  }
  assert {
    condition     = module.bronze_to_silver.job_arguments_for_test["--silver-database"] == "procurelake_dev_silver"
    error_message = "The job must write the foundation's Silver database."
  }
  assert {
    condition     = strcontains(module.bronze_to_silver.job_arguments_for_test["--additional-python-modules"], ",pydantic==") && strcontains(module.bronze_to_silver.job_arguments_for_test["--additional-python-modules"], ",pyyaml==")
    error_message = "Pins from jobs/glue/silver_python_modules.txt must be passed to the job."
  }
}

run "silver_job_cannot_delete_bronze" {
  command = apply

  assert {
    condition = anytrue([
      for s in jsondecode(local.bronze_to_silver_policy).Statement :
      s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*")
    ])
    error_message = "The Silver job role must be denied deleting Bronze objects."
  }
  assert {
    condition = alltrue([
      for s in jsondecode(local.bronze_to_silver_policy).Statement :
      s.Effect == "Deny" || !contains(flatten([s.Resource]), "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/*")
    ])
    error_message = "No allow statement may cover the whole raw bucket."
  }
  assert {
    condition = alltrue(flatten([
      for s in jsondecode(local.bronze_to_silver_policy).Statement : [
        for r in flatten([s.Resource]) : !endswith(r, ":*") && r != "*"
      ]
    ]))
    error_message = "No wildcard-only resources."
  }
}

run "cdc_job_uses_the_checkpoint_table_and_cannot_delete_bronze" {
  command = apply

  assert {
    condition     = module.apply_cdc.job_arguments_for_test["--checkpoint-table"] == "procurelake-dev-cdc-checkpoints" && output.apply_cdc_job_name == "procurelake-dev-apply-cdc"
    error_message = "The CDC job must use the checkpoint table."
  }
  assert {
    condition     = aws_dynamodb_table.cdc_checkpoints.deletion_protection_enabled && aws_dynamodb_table.cdc_checkpoints.server_side_encryption[0].enabled && aws_dynamodb_table.cdc_checkpoints.point_in_time_recovery[0].enabled
    error_message = "Checkpoints must be protected from deletion, encrypted and recoverable."
  }
  assert {
    condition = anytrue([for s in jsondecode(local.apply_cdc_policy).Statement :
    s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*")])
    error_message = "The CDC job role must be denied Bronze deletes."
  }
  assert {
    condition = alltrue([for s in jsondecode(local.apply_cdc_policy).Statement :
    s.Effect == "Deny" || !contains(flatten([s.Resource]), "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/postgres_cdc/*") || flatten([s.Action]) == ["s3:GetObject"]])
    error_message = "The CDC job may only read DMS output in Bronze."
  }
  assert {
    condition     = module.apply_cdc.job_arguments_for_test["--conf"] == module.bronze_to_silver.job_arguments_for_test["--conf"]
    error_message = "Both Silver writers must use the same Iceberg catalog configuration."
  }
}

run "no_state_machines_before_the_dbt_image_exists" {
  command = apply

  assert {
    condition     = length(aws_sfn_state_machine.cdc_processing) == 0 && length(aws_sfn_state_machine.dbt_transformation) == 0
    error_message = "State machines need a dbt runner task definition (image digest)."
  }
}

run "state_machines_fail_loudly_and_retry_only_idempotent_work" {
  command = apply

  variables {
    dbt_image_digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.dbt_transformation[0].definition).States.CheckExitCode.Default == "DbtFailed"
    error_message = "dbt_transformation must fail unless the container exits 0."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.dbt_transformation[0].definition).States.RunDbt.Retry[0].ErrorEquals == ["ECS.AmazonECSException", "AmazonECS.Unknown"]
    error_message = "A dbt failure must not be retried; only ECS capacity/API errors."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.cdc_processing[0].definition).States.ApplyCdc.Parameters.JobName == "procurelake-dev-apply-cdc" && jsondecode(aws_sfn_state_machine.cdc_processing[0].definition).States.ApplyCdc.Catch[0].Next == "NotifyFailure"
    error_message = "cdc_processing must run the CDC apply job and notify on failure."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.cdc_processing[0].definition).States.AnotherExecutionRunning.Choices[0].Next == "SkippedBecauseAnotherIsRunning"
    error_message = "Overlapping executions must skip themselves."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.dbt_transformation[0].policy).Statement :
    s.Action == "iam:PassRole" && s.Condition.StringEquals["iam:PassedToService"] == "ecs-tasks.amazonaws.com"])
    error_message = "PassRole must be limited to ECS tasks."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.dbt_transformation[0].definition).States.CheckExitCode.Choices[0].Next == "CheckTenantAccess" && jsondecode(aws_sfn_state_machine.dbt_transformation[0].definition).States.CheckTenantAccess.Parameters.StateMachineArn == "arn:aws:states:ca-central-1:123456789012:stateMachine:procurelake-dev-tenant-access-check"
    error_message = "A successful dbt build must be followed by the tenant access check (ADR 0030)."
  }
}

run "ai_extraction_is_limited_to_the_canada_profile_and_its_own_paths" {
  command = apply

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.ai_extraction.policy).Statement :
      alltrue([for r in flatten([s.Resource]) : !startswith(r, "arn:aws:bedrock") || length(regexall("^arn:aws:bedrock:ca-(central|west)-1:", r)) > 0])
    ])
    error_message = "Bedrock access must be limited to Canadian Regions."
  }
  assert {
    condition = contains(
      flatten([for s in jsondecode(aws_iam_role_policy.ai_extraction.policy).Statement : s.Resource if s.Sid == "InvokeClaudeThroughCanadaProfile"]),
      "arn:aws:bedrock:ca-central-1:123456789012:inference-profile/ca.anthropic.claude-haiku-5-5"
    )
    error_message = "The role must invoke the model through the Canada inference profile."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.ai_extraction.policy).Statement :
      alltrue([for r in flatten([s.Resource]) :
        !startswith(r, "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1/") || r == "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1/silver/contract_intelligence/*"
      ])
    ])
    error_message = "The AI role may touch only its own Silver table's files in the lake."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.ai_extraction.policy).Statement :
    s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && s.Resource == "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/*"])
    error_message = "The AI role must be denied Bronze deletes."
  }
  assert {
    condition     = aws_dynamodb_table.ai_review.deletion_protection_enabled && aws_dynamodb_table.ai_review.server_side_encryption[0].enabled && aws_dynamodb_table.ai_review.point_in_time_recovery[0].enabled
    error_message = "The review queue must be protected from deletion, encrypted and recoverable."
  }
}

run "ai_profile_must_be_canadian" {
  command = plan

  variables {
    ai_inference_profile_id = "global.anthropic.claude-haiku-5-5"
  }
  expect_failures = [var.ai_inference_profile_id]
}

run "iceberg_maintenance_is_scheduled_alerting_and_cannot_touch_bronze" {
  command = apply

  assert {
    condition     = module.iceberg_maintenance.job_arguments_for_test["--silver-max-age-seconds"] == "604800" && module.iceberg_maintenance.job_arguments_for_test["--silver-min-snapshots"] == "10" && module.iceberg_maintenance.job_arguments_for_test["--gold-max-age-seconds"] == "86400" && module.iceberg_maintenance.job_arguments_for_test["--gold-min-snapshots"] == "5"
    error_message = "Retention must match the owner decision: Silver 7 days/10 snapshots, Gold 1 day/5."
  }
  assert {
    condition     = alltrue([for t in split(",", module.iceberg_maintenance.job_arguments_for_test["--leased-tables"]) : contains(split(",", module.iceberg_maintenance.job_arguments_for_test["--silver-tables"]), t)])
    error_message = "Leased tables must be maintained Silver tables."
  }
  assert {
    condition = anytrue([for s in jsondecode(local.maintenance_policy).Statement :
    s.Effect == "Deny" && s.Action == "s3:*" && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/*")])
    error_message = "Maintenance must be denied all access to Bronze."
  }
  assert {
    condition = jsonencode(one([for s in jsondecode(local.maintenance_policy).Statement :
    s.Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] if s.Sid == "CdcWriterLease"])) == jsonencode(var.maintenance_leased_tables)
    error_message = "Maintenance may only take the CDC lease of the tables it maintains under lease."
  }
  assert {
    condition     = aws_scheduler_schedule.iceberg_maintenance.schedule_expression_timezone == "America/Toronto" && jsondecode(aws_scheduler_schedule.iceberg_maintenance.target[0].input).JobName == "procurelake-dev-iceberg-maintenance"
    error_message = "The daily schedule must start the maintenance job."
  }
  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.maintenance_failed.event_pattern).detail.jobName == ["procurelake-dev-iceberg-maintenance"] && aws_cloudwatch_event_target.maintenance_failed.arn == aws_sns_topic.pipeline_alerts.arn
    error_message = "A failed maintenance run must alert the pipeline topic."
  }
}

run "silver_retention_cannot_be_under_a_day" {
  command = plan

  variables {
    silver_snapshot_retention = { max_age_seconds = 3600, min_snapshots = 10 }
  }
  expect_failures = [var.silver_snapshot_retention]
}

run "bench_jobs_touch_only_the_bench_namespace" {
  command = apply

  assert {
    condition     = module.bench_bronze_to_silver.job_name == "procurelake-dev-bench-bronze-to-silver" && module.partition_evolution.job_name == "procurelake-dev-partition-evolution"
    error_message = "Unexpected benchmark job names."
  }
  # Every allowed resource is in the bench namespace: lake bench/, raw postgres_bench or
  # quarantine/bench, bench manifests/run summaries/reports, the bench Silver database, the
  # catalog itself, or the KMS key.
  assert {
    condition = alltrue(flatten([
      for doc in [local.bench_silver_policy, local.partition_evolution_policy] : [
        for s in jsondecode(doc).Statement : [
          for r in flatten([s.Resource]) : s.Effect == "Deny" || startswith(r, "arn:aws:kms:") || endswith(r, ":catalog") || contains([
            "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1",
            "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1",
            "arn:aws:s3:::procurelake-dev-artifacts-123456789012-ca-central-1",
          ], r) || strcontains(r, "bench")
        ]
      ]
    ]))
    error_message = "Benchmark roles may only be allowed on bench resources."
  }
  assert {
    condition = anytrue([
      for s in jsondecode(local.bench_silver_policy).Statement :
      s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*")
    ])
    error_message = "The bench Silver job must be denied deleting Bronze."
  }
  assert {
    condition = anytrue([
      for s in jsondecode(local.partition_evolution_policy).Statement :
      s.Effect == "Deny" && s.Action == "s3:*" && contains(s.Resource, "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/*")
    ])
    error_message = "The partition-evolution job must have no access to the raw bucket."
  }
  assert {
    condition     = module.bench_bronze_to_silver.job_arguments_for_test["--source-system"] == "postgres_bench"
    error_message = "The bench Silver job must read only postgres_bench batches."
  }
}

run "no_batch_or_quality_machines_before_the_task_runner_image_exists" {
  command = apply

  variables {
    dbt_image_digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
  }

  assert {
    condition     = length(aws_sfn_state_machine.batch_ingestion) == 0 && length(aws_sfn_state_machine.data_quality) == 0 && length(aws_scheduler_schedule.batch) == 0
    error_message = "Batch ingestion and data quality need the task runner image (ADR 0040)."
  }
}

run "batch_ingestion_and_data_quality_run_as_the_right_roles_and_alert" {
  command = apply

  variables {
    dbt_image_digest         = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    task_runner_image_digest = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
  }

  assert {
    condition     = jsondecode(module.task_runner.container_definitions_for_test)[0].name == "task"
    error_message = "The task runner container is named task (the state machines override it by name)."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.batch_ingestion[0].definition).States.CheckExitCode.Default == "Alert" && jsondecode(aws_sfn_state_machine.batch_ingestion[0].definition).States.ValidateInput.Default == "BadInput"
    error_message = "Batch ingestion must alert unless the task exits 0, and refuse unknown tasks."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.data_quality[0].definition).States.SilverChecks.Parameters.Overrides.TaskRoleArn == "arn:aws:iam::123456789012:role/procurelake-dev-data-engineer"
    error_message = "The Silver checks must run as the data engineer, not the ingestion role."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.data_quality[0].definition).States.DbtTest.Catch[0].Next == "SilverChecks" && jsondecode(aws_sfn_state_machine.data_quality[0].definition).States.Evaluate.Default == "Alert"
    error_message = "Both checks must always run, and any failure must alert."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.batch_ingestion[0].policy).Statement :
    s.Action != "iam:PassRole" || (s.Condition.StringEquals["iam:PassedToService"] == "ecs-tasks.amazonaws.com" && !contains(s.Resource, "arn:aws:iam::123456789012:role/procurelake-dev-data-engineer"))])
    error_message = "Batch ingestion may pass only the runner's execution role and the ingestion role, to ECS tasks."
  }
  assert {
    condition     = toset(keys(aws_scheduler_schedule.batch)) == toset(["usaspending", "usaspending-resweep", "canada-contracts", "data-quality"]) && aws_scheduler_schedule.batch["data-quality"].schedule_expression_timezone == "America/Toronto"
    error_message = "All four schedules exist by default, in Toronto time."
  }
  assert {
    condition     = jsondecode(aws_scheduler_schedule.batch["usaspending-resweep"].target[0].input).task == "resweep-usaspending" && aws_scheduler_schedule.batch["usaspending-resweep"].schedule_expression == "cron(0 4 ? * SUN *)"
    error_message = "The weekly re-sweep starts the resweep-usaspending task on Sundays after maintenance (ADR 0044)."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.batch_ingestion[0].definition).States.ApplyReferenceToSilver.Parameters.JobName == "procurelake-dev-reference-to-silver" && jsondecode(aws_sfn_state_machine.batch_ingestion[0].definition).States.ApplyReferenceToSilver.Catch[0].Next == "Alert"
    error_message = "USAspending ingestion continues into the Silver job, and a Silver failure alerts."
  }
}

run "reference_silver_job_touches_only_usaspending_data" {
  command = apply

  # Every allowed resource is a USAspending prefix, the job's own Silver table, a bucket (for
  # prefix-conditioned listing), the catalog or Silver database, or the KMS key.
  assert {
    condition = alltrue(flatten([
      for s in jsondecode(local.reference_to_silver_policy).Statement : [
        for r in flatten([s.Resource]) : s.Effect == "Deny" || startswith(r, "arn:aws:kms:") || endswith(r, ":catalog") || endswith(r, ":database/procurelake_dev_silver") || contains([
          "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1",
          "arn:aws:s3:::procurelake-dev-lake-123456789012-ca-central-1",
          "arn:aws:s3:::procurelake-dev-artifacts-123456789012-ca-central-1",
        ], r) || strcontains(r, "usaspending") || strcontains(r, "spending_by_award")
      ]
    ]))
    error_message = "The reference job may read and write only USAspending prefixes and its own Silver table."
  }
  assert {
    condition     = module.reference_to_silver.job_arguments_for_test["--contract"] == "usaspending_contract_awards" && module.reference_to_silver.job_arguments_for_test["--max-batches"] == "60"
    error_message = "The job applies the USAspending contract, at most 60 batches per run."
  }
}

run "an_empty_schedule_disables_it" {
  command = apply

  variables {
    dbt_image_digest          = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    task_runner_image_digest  = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
    schedule_canada_contracts = ""
  }

  assert {
    condition     = !contains(keys(aws_scheduler_schedule.batch), "canada-contracts") && length(aws_sfn_state_machine.batch_ingestion) == 1
    error_message = "An empty schedule turns that schedule off without removing the machine."
  }
}
