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

variable "maintenance_silver_tables" {
  description = "Silver Iceberg tables maintained daily (ADR 0033)."
  type        = list(string)
  default = [
    "silver_agencies", "silver_suppliers", "silver_solicitations", "silver_awards",
    "silver_purchase_orders", "silver_invoices", "silver_tenants", "silver_contract_intelligence",
  ]
}

variable "maintenance_leased_tables" {
  description = "Silver tables the CDC apply job writes: maintained only under their CDC lease."
  type        = list(string)
  default = [
    "silver_agencies", "silver_suppliers", "silver_solicitations", "silver_awards",
    "silver_purchase_orders", "silver_invoices", "silver_tenants",
  ]
}

variable "maintenance_gold_tables" {
  description = "Gold marts built with the Iceberg merge strategy (dbt rebuilds the other Gold tables in full)."
  type        = list(string)
  default     = ["mart_awarded_value", "mart_supplier_concentration", "mart_procurement_cycle_time", "mart_actual_spend"]
}

variable "silver_snapshot_retention" {
  description = "Silver snapshot retention (owner decision 2026-11-11): 7 days, at least 10 snapshots."
  type        = object({ max_age_seconds = number, min_snapshots = number })
  default     = { max_age_seconds = 604800, min_snapshots = 10 }
  validation {
    condition     = var.silver_snapshot_retention.max_age_seconds >= 86400 && var.silver_snapshot_retention.min_snapshots >= 1
    error_message = "Silver keeps at least one day of snapshots (CDC investigation and recovery)."
  }
}

variable "gold_snapshot_retention" {
  description = "Gold mart snapshot retention (owner decision 2026-11-11): 1 day, at least 5 snapshots."
  type        = object({ max_age_seconds = number, min_snapshots = number })
  default     = { max_age_seconds = 86400, min_snapshots = 5 }
  validation {
    condition     = var.gold_snapshot_retention.max_age_seconds >= 3600 && var.gold_snapshot_retention.min_snapshots >= 1
    error_message = "Gold keeps at least one hour and one snapshot."
  }
}

variable "maintenance_schedule" {
  description = "When the daily maintenance runs (America/Toronto)."
  type        = string
  default     = "cron(30 3 * * ? *)"
}
