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
  description = "Terraform state bucket (read-only here: foundation outputs)."
  type        = string
}

variable "data_lake_admin_arns" {
  description = "Lake Formation data lake administrators (the full list: settings replace it)."
  type        = list(string)

  validation {
    condition     = length(var.data_lake_admin_arns) > 0
    error_message = "At least one data lake administrator is required to grant permissions."
  }
}

variable "tenants" {
  description = "Tenants with a scoped consumer role (synthetic tenants, AGENTS.md section 2.3)."
  type        = list(string)
  default     = ["tenant_alder", "tenant_birch"]
}

variable "filtered_tables" {
  description = "Gold tables exposed to tenants through Lake Formation row filters: the tenant-facing marts (ADR 0030). They are dbt merge models, never dropped, so their grants persist."
  type        = list(string)
  default     = ["mart_awarded_value", "mart_actual_spend", "mart_procurement_cycle_time", "mart_supplier_concentration"]
}

variable "exports_schedule" {
  description = "EventBridge Scheduler expression for the daily tenant exports (America/Toronto)."
  type        = string
  default     = "cron(0 6 * * ? *)"
}
