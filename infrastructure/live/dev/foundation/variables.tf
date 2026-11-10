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

variable "tenants" {
  description = "Synthetic tenants (fictional, not SOVRA customers); must match dev/security."
  type        = list(string)
  default     = ["tenant_alder", "tenant_birch"]
}
