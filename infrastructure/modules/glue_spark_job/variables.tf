variable "name" {
  description = "Job and role name, e.g. 'procurelake-dev-bronze-to-silver'."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.name))
    error_message = "name must be lowercase letters, digits or hyphens."
  }
}

variable "description" {
  description = "Job description."
  type        = string
}

variable "aws_account_id" {
  description = "Account whose Glue service may assume the job role."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be 12 digits."
  }
}

variable "script_path" {
  description = "Local path of the thin entrypoint script (jobs/glue/*.py)."
  type        = string
}

variable "wheel_path" {
  description = "Local path of the built procurelake wheel (make glue-wheel)."
  type        = string
}

variable "python_modules" {
  description = "Exact 'name==version' pins installed with --no-deps next to the wheel."
  type        = list(string)

  validation {
    condition     = alltrue([for m in var.python_modules : can(regex("^[A-Za-z0-9._-]+==[A-Za-z0-9.+-]+$", m))])
    error_message = "Every python module must be pinned as name==version."
  }
}

variable "artifacts_bucket_name" {
  description = "Bucket for the script, wheel and temporary directory."
  type        = string
}

variable "artifacts_prefix" {
  description = "Prefix in the artifacts bucket for Glue code and temp files."
  type        = string
  default     = "glue"
}

variable "kms_key_arn" {
  description = "KMS key encrypting the artifacts bucket."
  type        = string
}

variable "data_access_policy_json" {
  description = "IAM policy document for the data the job reads and writes."
  type        = string
}

variable "job_arguments" {
  description = "Job-specific default arguments (merged over the module's)."
  type        = map(string)
  default     = {}
}

variable "glue_version" {
  description = "AWS Glue version (ADR 0015: 5.1, Spark 3.5.6, Python 3.11, Iceberg 1.10.0)."
  type        = string
  default     = "5.1"
}

variable "worker_type" {
  description = "Glue worker type."
  type        = string
  default     = "G.1X"
}

variable "number_of_workers" {
  description = "Number of workers (driver included)."
  type        = number
  default     = 2

  validation {
    condition     = var.number_of_workers >= 2 && var.number_of_workers <= 10
    error_message = "number_of_workers must be between 2 and 10 in dev."
  }
}

variable "timeout_minutes" {
  description = "Job run timeout."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "Retention for the Glue log groups."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
