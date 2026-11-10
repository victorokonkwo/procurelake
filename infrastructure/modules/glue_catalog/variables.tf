variable "name_prefix" {
  description = "Database name prefix using underscores, e.g. 'procurelake_dev'."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,40}$", var.name_prefix))
    error_message = "name_prefix must be lowercase letters, digits or underscores (Glue/Athena safe)."
  }
}

variable "environment" {
  description = "Environment name, used in descriptions."
  type        = string
}

variable "raw_bucket_name" {
  description = "Bucket holding Bronze data."
  type        = string
}

variable "lake_bucket_name" {
  description = "Bucket holding Silver and Gold Iceberg tables."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Bucket holding ingestion manifests."
  type        = string
}

variable "projection_start_date" {
  description = "Earliest ingestion_date for partition projection (yyyy-MM-dd)."
  type        = string
  default     = "2026-11-01"

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.projection_start_date))
    error_message = "projection_start_date must be yyyy-MM-dd."
  }
}

variable "data_owner" {
  description = "Owner recorded in table parameters (catalog governance)."
  type        = string
  default     = "data-platform"
}
