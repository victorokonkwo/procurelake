variable "name_prefix" {
  description = "Role name prefix, e.g. 'procurelake-dev'."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account whose principals may assume the roles."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "bucket_arns" {
  description = "Bucket ARNs keyed by role; must contain raw, lake, products and artifacts."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["raw", "lake", "products", "artifacts"] : contains(keys(var.bucket_arns), k)])
    error_message = "bucket_arns must contain keys raw, lake, products and artifacts."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key."
  type        = string
}

variable "trusted_service_principals" {
  description = "AWS service principals (e.g. glue.amazonaws.com) allowed to assume the roles."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}

variable "region" {
  description = "AWS region of the Glue catalog."
  type        = string
  default     = "ca-central-1"
}

variable "athena_workgroup_arns" {
  description = "Athena workgroups the data engineer may run queries in. Empty disables query access."
  type        = list(string)
  default     = []
}

variable "glue_database_names" {
  description = "Glue databases (and their tables) the data engineer may read metadata for."
  type        = list(string)
  default     = []
}

variable "glue_writable_database_names" {
  description = "Glue databases whose tables the data engineer may create, replace and drop (dbt Gold)."
  type        = list(string)
  default     = []
}

variable "ingestion_service_principals" {
  description = "Extra service principals that may assume the ingestion role only (e.g. ecs-tasks.amazonaws.com for batch ingestion, ADR 0040)."
  type        = list(string)
  default     = []
}

variable "data_engineer_service_principals" {
  description = "Extra service principals that may assume the data engineer role only (e.g. ecs-tasks.amazonaws.com)."
  type        = list(string)
  default     = []
}
