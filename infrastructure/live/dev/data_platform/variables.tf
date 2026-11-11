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

variable "ai_inference_profile_id" {
  description = "Bedrock inference profile for contract extraction (ADR 0032; configs/ai/model_config.yaml model_id)."
  type        = string
  default     = "ca.anthropic.claude-haiku-5-5"
  validation {
    condition     = startswith(var.ai_inference_profile_id, "ca.")
    error_message = "Contract extraction must use a Canada (ca.) inference profile so text stays in Canada (ADR 0001)."
  }
}

variable "ai_foundation_model_id" {
  description = "The foundation model behind ai_inference_profile_id."
  type        = string
  default     = "anthropic.claude-haiku-5-5"
}

variable "ai_inference_profile_regions" {
  description = "Regions the inference profile routes requests to (aws bedrock list-inference-profiles)."
  type        = list(string)
  default     = ["ca-central-1", "ca-west-1"]
  validation {
    condition     = alltrue([for r in var.ai_inference_profile_regions : startswith(r, "ca-")])
    error_message = "Only Canadian Regions."
  }
}
