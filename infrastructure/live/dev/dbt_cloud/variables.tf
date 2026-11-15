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

variable "dbt_cloud_ip_addresses" {
  description = <<-EOT
    The only addresses the dbt Cloud user's keys work from (ADR 0051). Default: the dbt platform
    North America AWS multi-tenant region (us-east-1, ACCOUNT_PREFIX.us1.dbt.com), the region of
    the free Developer plan, from docs.getdbt.com "Access, Regions, & IP addresses" (page updated
    2026-09-09, read 2026-11-14). dbt says these can change: if dbt Cloud starts getting
    AccessDenied, compare this list with that page.
  EOT
  type        = list(string)
  default = [
    "52.45.144.63/32",
    "54.81.134.249/32",
    "52.22.161.231/32",
    "52.3.77.232/32",
    "3.214.191.130/32",
    "34.233.79.135/32",
  ]

  validation {
    condition     = length(var.dbt_cloud_ip_addresses) > 0 && alltrue([for c in var.dbt_cloud_ip_addresses : can(cidrhost(c, 0)) && !startswith(c, "0.0.0.0/")])
    error_message = "dbt_cloud_ip_addresses must be a non-empty list of CIDR blocks, and none may be 0.0.0.0/x."
  }
}

variable "bytes_scanned_cutoff_per_query" {
  description = "Athena per-query scan limit of the dbt Cloud workgroup."
  type        = number
  default     = 1073741824 # 1 GiB, as the engineering workgroup
}
