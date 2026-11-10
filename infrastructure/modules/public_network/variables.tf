variable "name" {
  description = "Name prefix."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR (/16)."
  type        = string
  default     = "10.50.0.0/16"
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
