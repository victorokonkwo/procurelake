variable "name_prefix" {
  description = "Bucket name prefix, e.g. 'procurelake-dev'."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,30}$", var.name_prefix))
    error_message = "name_prefix must be 3-31 lowercase letters, digits or hyphens."
  }
}

variable "aws_account_id" {
  description = "AWS account ID, included in bucket names for global uniqueness."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "region" {
  description = "AWS region, included in bucket names."
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS key used for default bucket encryption."
  type        = string
}

variable "raw_noncurrent_version_expiration_days" {
  description = "Days to keep overwritten/deleted object versions in the raw bucket."
  type        = number
  default     = 365
}

variable "athena_results_expiration_days" {
  description = "Days to keep Athena query results under artifacts/athena_results/."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}

variable "products_tenant_prefixes" {
  description = "Tenant export prefixes in the products bucket, mapped to the only tenant role allowed to touch them. Every other tenant role is denied that prefix in the bucket policy (ADR 0030). Roles are named, not referenced, so this module does not depend on dev/security."
  type        = map(string)
  default     = {}
}

variable "eventbridge_buckets" {
  description = "Bucket keys (raw, lake, products, artifacts) whose object events are sent to Amazon EventBridge (S3 bucket notification `eventbridge = true`). Consumers filter with EventBridge rules (e.g. the document intelligence trigger on artifacts, ADR 0045)."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for b in var.eventbridge_buckets : contains(["raw", "lake", "products", "artifacts"], b)])
    error_message = "eventbridge_buckets may only name raw, lake, products or artifacts."
  }
}
