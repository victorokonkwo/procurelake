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
  description = "Terraform state bucket (read-only here: data_platform and security outputs)."
  type        = string
}

variable "freshness_threshold_seconds" {
  description = "Alarm when a CDC batch reaches Silver later than this after its oldest source commit. The C5 gate is 15 minutes to Gold (ADR 0027)."
  type        = number
  default     = 900
}
