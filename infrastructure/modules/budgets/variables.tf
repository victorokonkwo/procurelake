variable "name" {
  description = "Budget name."
  type        = string
}

variable "monthly_limit_usd" {
  description = "Monthly cost budget in USD."
  type        = number

  validation {
    condition     = var.monthly_limit_usd > 0
    error_message = "monthly_limit_usd must be positive."
  }
}

variable "alert_emails" {
  description = "Email addresses notified when a threshold is crossed."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0 && alltrue([for e in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
    error_message = "Provide at least one valid email address."
  }
}

variable "actual_spend_thresholds_percent" {
  description = "Percentages of the limit that trigger an alert on actual spend."
  type        = list(number)
  default     = [50, 80, 100]
}

variable "forecasted_spend_thresholds_percent" {
  description = "Percentages of the limit that trigger an alert on forecasted spend."
  type        = list(number)
  default     = [100]
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
