mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_dms_replication_instance" {
    defaults = { replication_instance_arn = "arn:aws:dms:ca-central-1:123456789012:rep:MOCKINSTANCE" }
  }
  mock_resource "aws_dms_endpoint" {
    defaults = { endpoint_arn = "arn:aws:dms:ca-central-1:123456789012:endpoint:MOCKSOURCE" }
  }
  mock_resource "aws_dms_s3_endpoint" {
    defaults = { endpoint_arn = "arn:aws:dms:ca-central-1:123456789012:endpoint:MOCKTARGET" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-cdc-x" }
  }
}

variables {
  name                   = "procurelake-dev-cdc"
  aws_account_id         = "123456789012"
  region                 = "ca-central-1"
  subnet_ids             = ["subnet-a", "subnet-b"]
  security_group_id      = "sg-1"
  replication_secret_arn = "arn:aws:secretsmanager:ca-central-1:123456789012:secret:procurelake-dev-cdc-source/replication-user"
  database_name          = "procurelake"
  raw_bucket_name        = "procurelake-dev-raw-123456789012-ca-central-1"
  kms_key_arn            = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "bronze_output_is_parquet_with_commit_timestamps_and_ops" {
  command = apply

  assert {
    condition     = aws_dms_s3_endpoint.target.data_format == "parquet" && aws_dms_s3_endpoint.target.timestamp_column_name == "_dms_commit_ts" && aws_dms_s3_endpoint.target.include_op_for_full_load
    error_message = "DMS must write Parquet with the commit timestamp column and operation flags."
  }
  assert {
    condition     = aws_dms_s3_endpoint.target.encryption_mode == "SSE_KMS" && aws_dms_s3_endpoint.target.server_side_encryption_kms_key_id == var.kms_key_arn
    error_message = "CDC files must be encrypted with the platform data key."
  }
  assert {
    condition     = startswith(aws_dms_s3_endpoint.target.bucket_folder, "bronze/postgres_cdc/load_id=")
    error_message = "Each stack instance must write under its own load_id prefix."
  }
  assert {
    condition     = jsondecode(aws_dms_replication_task.this.replication_task_settings).FullLoadSettings.TargetTablePrepMode == "DO_NOTHING"
    error_message = "DMS must never prepare (delete) target files."
  }
}

run "dms_cannot_delete_bronze" {
  command = apply

  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.s3_target.policy).Statement :
    s.Effect == "Deny" && contains(s.Action, "s3:DeleteObject") && s.Resource == "arn:aws:s3:::procurelake-dev-raw-123456789012-ca-central-1/bronze/*"])
    error_message = "The DMS S3 role must be denied Bronze deletes."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.s3_target.policy).Statement :
    s.Effect == "Deny" || !contains(flatten([s.Action]), "s3:DeleteObject")])
    error_message = "No allow statement may grant deletes."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.s3_target.policy).Statement :
    s.Effect == "Deny" || !contains(flatten([s.Action]), "s3:GetObject")])
    error_message = "The DMS role writes Bronze; it never reads objects."
  }
  assert {
    condition = anytrue([for s in jsondecode(aws_iam_role_policy.s3_target.policy).Statement :
    s.Action == "s3:ListBucket" && !can(s.Condition)])
    error_message = "HeadBucket needs s3:ListBucket without a prefix condition."
  }
}

run "replication_is_private_and_ddl_free" {
  command = apply

  assert {
    condition     = !aws_dms_replication_instance.this.publicly_accessible && aws_dms_replication_instance.this.engine_version == "3.6.1"
    error_message = "Replication instance must be private, on an engine that supports PostgreSQL 17."
  }
  assert {
    condition     = !aws_dms_endpoint.source.postgres_settings[0].capture_ddls && aws_dms_endpoint.source.ssl_mode == "require"
    error_message = "No DDL artifacts on the source; TLS required."
  }
  assert {
    condition     = aws_dms_endpoint.source.postgres_settings[0].heartbeat_schema == "awsdms"
    error_message = "The heartbeat table must live in the dedicated awsdms schema, not public."
  }
  assert {
    condition     = jsondecode(aws_iam_role.secrets.assume_role_policy).Statement[0].Principal.Service == "dms.ca-central-1.amazonaws.com"
    error_message = "Secret access role must trust the regional DMS principal."
  }
}
