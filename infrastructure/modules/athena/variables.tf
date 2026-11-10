variable "name" {
  description = "Workgroup name."
  type        = string
}

variable "description" {
  description = "Workgroup description."
  type        = string
  default     = ""
}

variable "results_bucket_name" {
  description = "Bucket for query results (written under athena_results/<workgroup>/)."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key for result encryption."
  type        = string
}

variable "bytes_scanned_cutoff_per_query" {
  description = "Athena cancels a query that scans more than this many bytes (minimum 10 MB)."
  type        = number
  default     = 1073741824 # 1 GiB

  validation {
    condition     = var.bytes_scanned_cutoff_per_query >= 10485760
    error_message = "Athena requires a cutoff of at least 10485760 bytes (10 MB)."
  }
}

variable "named_queries" {
  description = "Saved queries to publish in the workgroup."
  type = map(object({
    description = string
    database    = string
    query       = string
  }))
  default = {}
}
