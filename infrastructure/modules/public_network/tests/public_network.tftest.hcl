mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["ca-central-1a", "ca-central-1b"] }
  }
}

variables {
  name = "procurelake-dev-orchestration"
}

run "two_public_subnets_no_nat_no_auto_public_ip" {
  command = apply

  assert {
    condition     = length(aws_subnet.public) == 2 && alltrue([for s in aws_subnet.public : !s.map_public_ip_on_launch])
    error_message = "Two public subnets; public IPs only where a task asks for one."
  }
  assert {
    condition     = aws_route.internet.gateway_id == aws_internet_gateway.this.id
    error_message = "The default route goes to the internet gateway (no NAT gateway)."
  }
}
