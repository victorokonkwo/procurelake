output "tenant_role_arns" {
  description = "Consumer role per tenant."
  value       = { for t, r in aws_iam_role.tenant : t => r.arn }
}

output "tenant_workgroups" {
  description = "Athena workgroup per tenant."
  value       = { for t, m in module.tenant_workgroup : t => m.workgroup_name }
}

output "gold_registered_location" {
  description = "Gold S3 location registered with Lake Formation (hybrid access mode)."
  value       = aws_lakeformation_resource.gold.arn
}
