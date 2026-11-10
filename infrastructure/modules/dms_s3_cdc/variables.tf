variable "name" {
  description = "Name prefix, e.g. 'procurelake-dev-cdc'."
  type        = string
}

variable "aws_account_id" {
  description = "Account whose DMS service may assume the roles."
  type        = string
}

variable "region" {
  description = "Region (regional DMS principal for the secret access role)."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the replication subnet group."
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group for the replication instance."
  type        = string
}

variable "replication_secret_arn" {
  description = "Secret with the replication user's username, password, host and port."
  type        = string
}

variable "database_name" {
  description = "Source database name."
  type        = string
}

variable "source_schema" {
  description = "Source schema to replicate."
  type        = string
  default     = "procurement"
}

variable "raw_bucket_name" {
  description = "Bronze bucket."
  type        = string
}

variable "bronze_prefix" {
  description = "Bronze prefix for DMS output."
  type        = string
  default     = "bronze/postgres_cdc"

  validation {
    condition     = startswith(var.bronze_prefix, "bronze/") && !endswith(var.bronze_prefix, "/")
    error_message = "bronze_prefix must be under bronze/ without a trailing slash."
  }
}

variable "kms_key_arn" {
  description = "KMS key for Bronze objects, the replication instance and the secret."
  type        = string
}

variable "commit_timestamp_column" {
  description = "Column DMS fills with the source commit timestamp (CDC) or task start (full load)."
  type        = string
  default     = "_dms_commit_ts"
}

variable "cdc_max_batch_interval_seconds" {
  description = "Maximum seconds DMS buffers CDC changes before writing a file (freshness)."
  type        = number
  default     = 60
}

variable "instance_class" {
  # dms.t3.micro is priced but not orderable in ca-central-1 for engines 3.5.4/3.6.1
  # (describe-orderable-replication-instances, 2026-11-10); dms.t3.small is the smallest.
  description = "Replication instance class."
  type        = string
  default     = "dms.t3.small"
}

variable "engine_version" {
  description = "DMS engine version (PostgreSQL 17 needs 3.5.4 or later)."
  type        = string
  default     = "3.6.1"
}

variable "start_task" {
  description = "Start the replication task when it is created."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}

variable "artifacts_schema" {
  description = "Source schema where DMS creates its heartbeat table (prepared by cdc-source-setup)."
  type        = string
  default     = "awsdms"
}
