mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["ca-central-1a", "ca-central-1b", "ca-central-1d"] }
  }
}

variables {
  name   = "procurelake-dev-cdc"
  region = "ca-central-1"
}

run "private_subnets_have_no_route_to_the_internet" {
  command = apply

  assert {
    condition     = length(aws_route_table.private.route) == 0
    error_message = "The private route table must have no routes (no NAT, no IGW)."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway" && contains(aws_vpc_endpoint.s3.route_table_ids, aws_route_table.private.id)
    error_message = "DMS must reach S3 through the gateway endpoint."
  }
  assert {
    condition     = aws_vpc_endpoint.secretsmanager.private_dns_enabled && length(aws_vpc_endpoint.secretsmanager.subnet_ids) == 1
    error_message = "Secrets Manager must be reachable privately, from one billed subnet."
  }
}

run "subnets_span_two_azs_without_public_ips" {
  command = apply

  assert {
    condition     = length(distinct(aws_subnet.private[*].availability_zone)) == 2 && length(distinct(aws_subnet.public[*].availability_zone)) == 2
    error_message = "RDS and DMS subnet groups need two AZs."
  }
  assert {
    condition     = alltrue([for s in concat(aws_subnet.public, aws_subnet.private) : !s.map_public_ip_on_launch])
    error_message = "No subnet may assign public IPs automatically."
  }
}
