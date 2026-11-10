variable "name" {
  description = "Name of the repository, cluster and task family."
  type        = string
}

variable "aws_account_id" {
  description = "Account whose ECS tasks may assume the execution role."
  type        = string
}

variable "region" {
  description = "Region (log configuration)."
  type        = string
}

variable "vpc_id" {
  description = "VPC for the task security group."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key for the ECR repository."
  type        = string
}

variable "task_role_arn" {
  description = "Role the dbt process runs as (must trust ecs-tasks.amazonaws.com)."
  type        = string
}

variable "image_digest" {
  description = "sha256 digest of the image to run (make dbt-image). Empty: no task definition yet."
  type        = string
  default     = ""

  validation {
    condition     = var.image_digest == "" || can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be empty or sha256:<64 hex>."
  }
}

variable "environment" {
  description = "Container environment (dbt profile settings)."
  type        = map(string)
  default     = {}
}

variable "cpu" {
  description = "Task CPU units."
  type        = number
  default     = 512
}

variable "memory" {
  description = "Task memory (MiB)."
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "Log retention."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
