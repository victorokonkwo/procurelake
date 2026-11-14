variable "description" {
  description = "Human-readable purpose of the key."
  type        = string
}

variable "alias" {
  description = "Alias name without the 'alias/' prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9/_-]+$", var.alias)) && !startswith(var.alias, "aws/")
    error_message = "Alias may contain only letters, digits, '/', '_' and '-', and must not start with 'aws/'."
  }
}

variable "deletion_window_in_days" {
  description = "Waiting period before a scheduled key deletion completes (7-30 days)."
  type        = number
  default     = 30

  validation {
    condition     = var.deletion_window_in_days >= 7 && var.deletion_window_in_days <= 30
    error_message = "deletion_window_in_days must be between 7 and 30."
  }
}

variable "tags" {
  description = "Additional tags for the key."
  type        = map(string)
  default     = {}
}

variable "sns_publishing_services" {
  description = "AWS service principal names (e.g. cloudwatch, events) allowed to use the key through SNS, to publish to topics encrypted with it."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for s in var.sns_publishing_services : contains(["cloudwatch", "events"], s)])
    error_message = "Only cloudwatch and events are expected to publish to the platform's encrypted topics."
  }
}

variable "cloudwatch_logs_log_group_patterns" {
  description = "Log group names (ArnLike wildcards allowed) of this account and region that CloudWatch Logs may encrypt with the key (ADR 0048)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for p in var.cloudwatch_logs_log_group_patterns : can(regex("^/[A-Za-z0-9_/.#*-]+$", p)) && p != "/*"])
    error_message = "Each pattern must be an absolute log group name, optionally with wildcards, and not every log group."
  }
}
