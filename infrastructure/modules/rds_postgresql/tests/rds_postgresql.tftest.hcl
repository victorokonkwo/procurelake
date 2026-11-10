mock_provider "aws" {
  mock_resource "aws_db_instance" {
    defaults = {
      address            = "procurelake-dev-cdc-source.example.ca-central-1.rds.amazonaws.com"
      port               = 5432
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:ca-central-1:123456789012:secret:rds!db-1", kms_key_id = "k", secret_status = "active" }]
    }
  }
}

variables {
  name           = "procurelake-dev-cdc-source"
  vpc_id         = "vpc-123"
  subnet_ids     = ["subnet-a", "subnet-b"]
  operator_cidrs = ["203.0.113.7/32"]
  kms_key_arn    = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
}

run "source_is_logical_replication_ready_and_tls_only" {
  command = apply

  assert {
    condition     = contains([for p in aws_db_parameter_group.this.parameter : "${p.name}=${p.value}"], "rds.logical_replication=1") && contains([for p in aws_db_parameter_group.this.parameter : "${p.name}=${p.value}"], "rds.force_ssl=1")
    error_message = "Logical replication and TLS must be enabled."
  }
  assert {
    condition     = aws_db_instance.this.engine_version == "17.11" && aws_db_instance.this.storage_encrypted && aws_db_instance.this.manage_master_user_password
    error_message = "PostgreSQL 17.11, encrypted, RDS-managed master password."
  }
}

run "only_the_operator_and_dms_can_connect" {
  command = apply

  assert {
    condition     = [for r in aws_vpc_security_group_ingress_rule.operator : r.cidr_ipv4] == ["203.0.113.7/32"]
    error_message = "Only the operator CIDR may connect from outside."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.replication.referenced_security_group_id == aws_security_group.replication.id
    error_message = "DMS must connect through its own security group."
  }
}

run "rejects_open_cidr" {
  command = plan

  variables {
    operator_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.operator_cidrs]
}
