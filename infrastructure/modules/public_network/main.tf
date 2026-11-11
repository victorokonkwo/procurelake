# Minimal network for short-lived Fargate tasks (dbt runner, ADR 0029): two public subnets with
# an internet gateway and no NAT gateway. Tasks get a public IP on their ENI, which AWS documents
# as one way for a Fargate task to reach ECR and AWS APIs ("the task must have a route to the
# internet"). Nothing listens: task security groups allow no ingress. Free while idle.

data "aws_availability_zones" "available" {
  # checkov:skip=CKV_AWS_394: only the first two available zone names are used for subnets; a changed list would show up as a planned subnet replacement, never silently
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = var.name })
}

# The VPC's default security group, managed with no rules: it denies all traffic, so anything
# launched without an explicit group gets no network access. Every workload here has its own
# group (Checkov CKV2_AWS_12; ADR 0037).
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-default-deny" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  cidr_block              = cidrsubnet(var.cidr_block, 8, count.index)
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name}-public-${count.index}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-public" })
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
