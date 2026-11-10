variable "name" {
  description = "Name prefix, e.g. 'procurelake-dev-cdc'."
  type        = string
}

variable "region" {
  description = "AWS region (endpoint service names)."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR (/16); subnets are /24s carved from it."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.cidr_block)) && endswith(var.cidr_block, "/16")
    error_message = "cidr_block must be a /16."
  }
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
