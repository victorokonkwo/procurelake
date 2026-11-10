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
