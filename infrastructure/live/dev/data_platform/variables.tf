variable "aws_account_id" {
  description = "The only AWS account this stack may modify."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "region" {
  description = "Primary region (ADR 0001)."
  type        = string
  default     = "ca-central-1"
}

variable "state_bucket" {
  description = "Terraform state bucket holding dev/foundation state (read-only here)."
  type        = string
}

variable "dist_dir" {
  description = "Directory holding exactly one built procurelake wheel (make glue-wheel)."
  type        = string
  default     = "../../../../dist"
}

variable "dbt_image_digest" {
  description = "Digest of the dbt runner image in ECR (make dbt-image). Empty until the first push."
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Optional email subscribed to pipeline failure alerts (git-ignored tfvars)."
  type        = string
  default     = ""
}
