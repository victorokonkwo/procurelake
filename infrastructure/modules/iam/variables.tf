variable "name_prefix" {
  description = "Role name prefix, e.g. 'procurelake-dev'."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account whose principals may assume the roles."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "bucket_arns" {
  description = "Bucket ARNs keyed by role; must contain raw, lake, products and artifacts."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["raw", "lake", "products", "artifacts"] : contains(keys(var.bucket_arns), k)])
    error_message = "bucket_arns must contain keys raw, lake, products and artifacts."
  }
}

variable "kms_key_arn" {
  description = "ARN of the platform KMS key."
  type        = string
}

variable "trusted_service_principals" {
  description = "AWS service principals (e.g. glue.amazonaws.com) allowed to assume the roles."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
