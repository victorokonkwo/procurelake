output "address" {
  description = "Database host name."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Database port."
  value       = aws_db_instance.this.port
}

output "database_name" {
  description = "Database name."
  value       = aws_db_instance.this.db_name
}

output "master_user_secret_arn" {
  description = "RDS-managed secret holding the master credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "replication_secret_arn" {
  description = "Secret holding the DMS user's credentials (username, password, host, port)."
  value       = aws_secretsmanager_secret.replication.arn
}

output "replication_username" {
  description = "PostgreSQL user DMS connects as."
  value       = var.replication_username
}

output "replication_security_group_id" {
  description = "Security group for the DMS replication instance."
  value       = aws_security_group.replication.id
}
