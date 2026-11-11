variable "aws_account_id" {
  description = "The only AWS account this stack may modify (provider allowed_account_ids guard)."
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

variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget in USD. Alerts notify only; they do not cap spending."
  type        = number
  default     = 50
}

variable "budget_alert_emails" {
  description = "Recipients of budget alerts. Keep these in the git-ignored terraform.tfvars."
  type        = list(string)
}

variable "github_repository" {
  description = "GitHub repository allowed to federate via OIDC, as owner/name."
  type        = string
  default     = "victorokonkwo/procurelake"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be in owner/name form."
  }
}

variable "github_oidc_subject_prefix" {
  description = <<-EOT
    Prefix of the OIDC `sub` claim GitHub issues for this repository, as reported by
    `gh api repos/<owner>/<name>/actions/oidc/customization/sub` (sub_claim_prefix). This
    repository uses GitHub's immutable subject, `repo:<owner>@<owner id>/<name>@<repo id>`,
    which still matches after a rename (checked 2026-11-11, ADR 0035).
  EOT
  type        = string
  default     = "repo:victorokonkwo@42738051/procurelake@1452285010"

  validation {
    condition     = can(regex("^repo:[A-Za-z0-9_.-]+(@[0-9]+)?/[A-Za-z0-9_.-]+(@[0-9]+)?$", var.github_oidc_subject_prefix))
    error_message = "github_oidc_subject_prefix must look like repo:owner/name or repo:owner@id/name@id."
  }
}

variable "github_integration_branch" {
  description = "Branch whose pushes may run terraform plan via the plan role."
  type        = string
  default     = "dev"
}

variable "state_noncurrent_version_retention_days" {
  description = "Days to keep superseded Terraform state versions."
  type        = number
  default     = 365
}
