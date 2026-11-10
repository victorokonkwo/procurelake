variable "aws_account_id" {
  description = "Account whose DMS service may assume the roles."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
