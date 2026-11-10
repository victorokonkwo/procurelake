output "replication_task_arn" {
  description = "Replication task ARN."
  value       = aws_dms_replication_task.this.replication_task_arn
}

output "replication_task_id" {
  description = "Replication task ID."
  value       = aws_dms_replication_task.this.replication_task_id
}

output "bronze_cdc_prefix" {
  description = "Bronze prefix this stack instance writes to (includes its load_id)."
  value       = local.cdc_prefix
}

output "s3_target_policy_json" {
  description = "Policy of the DMS S3 role (asserted by tests)."
  value       = aws_iam_role_policy.s3_target.policy
}

output "cloudwatch_task_dimensions" {
  description = "Dimensions of the task's AWS/DMS CloudWatch metrics (CDCLatencySource etc.): the instance identifier and the task's resource identifier (the last part of its ARN). Checked live with list-metrics (docs/evidence, Week 8)."
  value = {
    ReplicationInstanceIdentifier = aws_dms_replication_instance.this.replication_instance_id
    ReplicationTaskIdentifier     = element(split(":", aws_dms_replication_task.this.replication_task_arn), 6)
  }
}
