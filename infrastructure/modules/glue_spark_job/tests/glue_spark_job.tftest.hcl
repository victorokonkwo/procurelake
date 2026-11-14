mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-test-job" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ca-central-1:123456789012:log-group:mock" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "ca-central-1" }
  }
}

variables {
  name                        = "procurelake-dev-test-job"
  description                 = "test"
  aws_account_id              = "123456789012"
  script_path                 = "../../../jobs/glue/bronze_to_silver.py"
  wheel_path                  = "tests/fixtures/procurelake-0.0.0-py3-none-any.whl"
  dependencies_bundle_path    = "tests/fixtures/procurelake-glue-dependencies.gluewheels.zip"
  artifacts_bucket_name       = "procurelake-dev-artifacts-123456789012-ca-central-1"
  kms_key_arn                 = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
  data_access_policy_json     = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  job_arguments               = { "--datalake-formats" = "iceberg" }
  security_configuration_name = "procurelake-dev-glue-jobs"
  log_kms_key_arn             = "arn:aws:kms:ca-central-1:123456789012:key/11111111-1111-1111-1111-111111111111"
}

run "job_runs_one_writer_without_retries_or_bookmarks" {
  command = apply

  assert {
    condition     = aws_glue_job.this.execution_property[0].max_concurrent_runs == 1 && aws_glue_job.this.max_retries == 0
    error_message = "A Silver job must have one concurrent run (one writer per table) and no automatic retries."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--job-bookmark-option"] == "job-bookmark-disable"
    error_message = "Bookmarks must be off: the job tracks applied batches itself."
  }
  assert {
    condition     = aws_glue_job.this.glue_version == "5.1" && aws_glue_job.this.command[0].name == "glueetl"
    error_message = "Silver jobs run on Glue 5.1 Spark (ADR 0015)."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--datalake-formats"] == "iceberg"
    error_message = "Job-specific arguments must be merged in."
  }
}

run "wheel_and_dependencies_are_content_addressed_and_installed_without_pypi" {
  command = apply

  assert {
    condition     = aws_s3_object.wheel.key == "glue/wheels/procurelake-dev-test-job/${filesha256("tests/fixtures/procurelake-0.0.0-py3-none-any.whl")}/procurelake-0.0.0-py3-none-any.whl"
    error_message = "Wheel key must contain the wheel's SHA-256."
  }
  assert {
    condition     = aws_s3_object.dependencies.key == "glue/wheels/procurelake-dev-test-job/${filesha256("tests/fixtures/procurelake-glue-dependencies.gluewheels.zip")}/procurelake-glue-dependencies.gluewheels.zip"
    error_message = "The dependency bundle key must contain its SHA-256 and keep the .gluewheels.zip suffix."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--additional-python-modules"] == "s3://procurelake-dev-artifacts-123456789012-ca-central-1/${aws_s3_object.wheel.key},s3://procurelake-dev-artifacts-123456789012-ca-central-1/${aws_s3_object.dependencies.key}"
    error_message = "Modules must be the wheel and the zip of wheels only: no name==version entry that pip would fetch from PyPI."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--python-modules-installer-option"] == "--no-index --no-deps"
    error_message = "pip must not contact an index (--no-index) nor resolve the wheel's own requirements (--no-deps)."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--dependencies-sha256"] == filesha256("tests/fixtures/procurelake-glue-dependencies.gluewheels.zip")
    error_message = "The job must record which dependency bundle it runs."
  }
  assert {
    condition = contains(
      flatten([for st in jsondecode(aws_iam_role_policy.runtime.policy).Statement : flatten([st.Resource]) if st.Sid == "ReadOwnCode"]),
      "arn:aws:s3:::procurelake-dev-artifacts-123456789012-ca-central-1/${aws_s3_object.dependencies.key}"
    )
    error_message = "The job role must be able to read its dependency bundle."
  }
  assert {
    condition     = startswith(aws_glue_job.this.default_arguments["--TempDir"], "s3://procurelake-dev-artifacts-123456789012-ca-central-1/glue/temp/")
    error_message = "TempDir must be in the platform artifacts bucket, not a Glue-created default bucket."
  }
}

run "role_is_trusted_by_glue_in_this_account_only" {
  command = apply

  assert {
    condition     = jsondecode(aws_iam_role.job.assume_role_policy).Statement[0].Principal.Service == "glue.amazonaws.com" && jsondecode(aws_iam_role.job.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    error_message = "Only the Glue service, acting for this account, may assume the job role."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role.job.assume_role_policy).Statement) == 1
    error_message = "No other principal may assume the job role."
  }
}

run "logs_are_encrypted_in_the_security_configuration_log_groups" {
  command = apply

  assert {
    condition     = aws_glue_job.this.security_configuration == "procurelake-dev-glue-jobs"
    error_message = "The job must run with the security configuration that encrypts its logs."
  }
  assert {
    condition = (
      aws_cloudwatch_log_group.job["error"].name == "/aws-glue/jobs/procurelake-dev-glue-jobs-role/procurelake-dev-test-job/error" &&
      aws_cloudwatch_log_group.job["output"].name == "/aws-glue/jobs/procurelake-dev-glue-jobs-role/procurelake-dev-test-job/output"
    )
    error_message = "Log groups must follow Glue's naming under a security configuration: /aws-glue/jobs/<config>-role/<role>/{error,output}."
  }
  assert {
    condition     = alltrue([for g in aws_cloudwatch_log_group.job : g.kms_key_id == "arn:aws:kms:ca-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" && g.retention_in_days == 30])
    error_message = "Both log groups must use the log key and keep 30 days."
  }
  assert {
    condition = anytrue([for st in jsondecode(aws_iam_role_policy.runtime.policy).Statement :
      st.Sid == "WriteJobLogs" && contains(st.Action, "logs:AssociateKmsKey") && toset(st.Resource) == toset(flatten([for g in aws_cloudwatch_log_group.job : [g.arn, "${g.arn}:*"]]))
    ])
    error_message = "The role writes only its own log groups and may associate the key with them (else Glue disables logging)."
  }
  assert {
    condition = anytrue([for st in jsondecode(aws_iam_role_policy.runtime.policy).Statement :
      st.Sid == "UseLogKeyThroughCloudWatchLogs" && st.Resource == "arn:aws:kms:ca-central-1:123456789012:key/11111111-1111-1111-1111-111111111111" && st.Condition.StringEquals["kms:ViaService"] == "logs.ca-central-1.amazonaws.com"
    ])
    error_message = "The role may use the log key only through CloudWatch Logs."
  }
}

run "rejects_a_bundle_without_the_gluewheels_suffix" {
  command = plan

  variables {
    dependencies_bundle_path = "tests/fixtures/procurelake-0.0.0-py3-none-any.whl"
  }
  expect_failures = [var.dependencies_bundle_path]
}
