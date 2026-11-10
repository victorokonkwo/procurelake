mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-test-job" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

variables {
  name                    = "procurelake-dev-test-job"
  description             = "test"
  aws_account_id          = "123456789012"
  script_path             = "../../../jobs/glue/bronze_to_silver.py"
  wheel_path              = "tests/fixtures/procurelake-0.0.0-py3-none-any.whl"
  python_modules          = ["pydantic==2.14.3", "pyyaml==6.0.3"]
  artifacts_bucket_name   = "procurelake-dev-artifacts-123456789012-ca-central-1"
  kms_key_arn             = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
  data_access_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  job_arguments           = { "--datalake-formats" = "iceberg" }
  log_group_arns          = ["arn:aws:logs:ca-central-1:123456789012:log-group:/aws-glue/jobs/error"]
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

run "wheel_is_content_addressed_and_installed_without_dependency_resolution" {
  command = apply

  assert {
    condition     = aws_s3_object.wheel.key == "glue/wheels/procurelake-dev-test-job/${filesha256("tests/fixtures/procurelake-0.0.0-py3-none-any.whl")}/procurelake-0.0.0-py3-none-any.whl"
    error_message = "Wheel key must contain the wheel's SHA-256."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--additional-python-modules"] == "s3://procurelake-dev-artifacts-123456789012-ca-central-1/${aws_s3_object.wheel.key},pydantic==2.14.3,pyyaml==6.0.3"
    error_message = "Modules must be the wheel followed by the exact pins."
  }
  assert {
    condition     = aws_glue_job.this.default_arguments["--python-modules-installer-option"] == "--no-deps"
    error_message = "pip must not resolve dependencies on its own (pins are the complete set)."
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
  assert {
    condition     = contains(flatten([for st in jsondecode(aws_iam_role_policy.runtime.policy).Statement : flatten([st.Resource])]), "arn:aws:logs:ca-central-1:123456789012:log-group:/aws-glue/jobs/error:*")
    error_message = "The job may write only to the given Glue log groups."
  }
}

run "rejects_unpinned_modules" {
  command = plan

  variables {
    python_modules = ["pydantic>=2"]
  }
  expect_failures = [var.python_modules]
}
