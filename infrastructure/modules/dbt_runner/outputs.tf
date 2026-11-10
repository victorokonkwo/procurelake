output "repository_url" {
  description = "ECR repository URL."
  value       = aws_ecr_repository.this.repository_url
}

output "cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.this.arn
}

output "task_definition_arn" {
  description = "Task definition ARN (null until an image digest is given)."
  value       = var.image_digest == "" ? null : aws_ecs_task_definition.this[0].arn
}

output "task_definition_family" {
  description = "Task definition family."
  value       = var.name
}

output "execution_role_arn" {
  description = "Task execution role ARN."
  value       = aws_iam_role.execution.arn
}

output "security_group_id" {
  description = "Task security group."
  value       = aws_security_group.task.id
}

output "container_definitions_for_test" {
  description = "Container definitions JSON (asserted by tests)."
  value       = var.image_digest == "" ? null : aws_ecs_task_definition.this[0].container_definitions
}
