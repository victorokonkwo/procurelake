variable "name" {
  description = "Identifier, e.g. 'procurelake-dev-cdc-source'."
  type        = string
}

variable "vpc_id" {
  description = "VPC for the database and replication security groups."
  type        = string
}

variable "subnet_ids" {
  description = "Public subnets for the DB subnet group (two AZs)."
  type        = list(string)
}

variable "operator_cidrs" {
  description = "CIDRs allowed to connect (the operator's IP as /32). Never 0.0.0.0/0."
  type        = list(string)

  validation {
    condition     = length(var.operator_cidrs) > 0 && alltrue([for c in var.operator_cidrs : can(cidrnetmask(c)) && tonumber(split("/", c)[1]) >= 24])
    error_message = "operator_cidrs must be specific CIDRs (/24 or narrower)."
  }
}

variable "kms_key_arn" {
  description = "KMS key for storage, the master secret and the replication secret."
  type        = string
}

variable "engine_version" {
  description = "PostgreSQL version (ADR 0015)."
  type        = string
  default     = "17.11"
}

variable "instance_class" {
  description = "Instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage_gb" {
  description = "gp3 storage (GB)."
  type        = number
  default     = 20
}

variable "database_name" {
  description = "Database name."
  type        = string
  default     = "procurelake"
}

variable "master_username" {
  description = "Master user (password managed by RDS)."
  type        = string
  default     = "procurelake_admin"
}

variable "replication_username" {
  description = "PostgreSQL user DMS connects as."
  type        = string
  default     = "procurelake_dms"
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
