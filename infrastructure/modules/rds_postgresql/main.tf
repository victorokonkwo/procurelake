# RDS for PostgreSQL as the CDC source (ADR 0027).
#
#   - PostgreSQL 17.11 (same as local, ADR 0015); logical replication on; TLS required.
#   - Publicly reachable for the operator's CIDR only (plus the DMS security group).
#   - Master password managed by RDS in Secrets Manager (never in Terraform state).
#   - Replication user secret for DMS: password generated ephemerally and written with a
#     write-only argument, so it is not stored in state either. DMS cannot use RDS-managed
#     secrets (AWS DMS User Guide), hence the separate user and secret.
#   - Disposable: no deletion protection, no final snapshot. The stack is destroyed after each
#     session; the source of truth for the data is the seeded generator plus Bronze.

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_db_parameter_group" "this" {
  name        = "${var.name}-pg17"
  family      = "postgres17"
  description = "Logical replication for AWS DMS CDC; TLS required."

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }
  # AWS records rds.force_ssl as pending-reboot; matching it keeps plans free of a phantom diff.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  tags = var.tags
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL from the operator CIDR and the DMS replication instance only."
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "operator" {
  for_each = toset(var.operator_cidrs)

  security_group_id = aws_security_group.db.id
  description       = "PostgreSQL (TLS) from the operator"
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_ingress_rule" "replication" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the DMS replication instance"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.replication.id
}

# Security group for the DMS replication instance, created here so the database rule can
# reference it before DMS exists. DMS needs all egress (AWS DMS User Guide).
resource "aws_security_group" "replication" {
  name        = "${var.name}-replication"
  description = "AWS DMS replication instance: no ingress; all egress (VPC endpoints, RDS)."
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_egress_rule" "replication_all" {
  security_group_id = aws_security_group.replication.id
  description       = "All egress (stays inside the VPC: no NAT, no internet route)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage = var.allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true
  kms_key_id        = var.kms_key_arn

  db_name                       = var.database_name
  username                      = var.master_username
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.kms_key_arn

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true
  multi_az               = false

  backup_retention_period    = 1
  auto_minor_version_upgrade = false
  apply_immediately          = true
  deletion_protection        = false
  skip_final_snapshot        = true
  copy_tags_to_snapshot      = true

  tags = var.tags
}

# ----------------------------------------------------------------------------- DMS user secret
ephemeral "aws_secretsmanager_random_password" "replication" {
  password_length = 32
  # Characters AWS DMS rejects in endpoint passwords, plus quoting-sensitive ones.
  exclude_characters  = ";.:+{}*&,%\\'\"`/@ "
  exclude_punctuation = false
}

resource "aws_secretsmanager_secret" "replication" {
  name                    = "${var.name}/replication-user"
  description             = "PostgreSQL user for AWS DMS (rds_replication + SELECT on the procurement schema)."
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 0
  tags                    = var.tags
}

resource "aws_secretsmanager_secret_version" "replication" {
  secret_id = aws_secretsmanager_secret.replication.id
  secret_string_wo = jsonencode({
    username = var.replication_username
    password = ephemeral.aws_secretsmanager_random_password.replication.random_password
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
  })
  secret_string_wo_version = 1
}
