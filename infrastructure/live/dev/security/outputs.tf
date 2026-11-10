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

output "admin_role_arn" {
  description = "Cross-tenant analytics administrator role."
  value       = aws_iam_role.admin.arn
}

output "admin_workgroup" {
  description = "Athena workgroup of the analytics administrator."
  value       = module.admin_workgroup.workgroup_name
}

output "tenant_exports_state_machine_arn" {
  description = "Daily tenant exports state machine."
  value       = aws_sfn_state_machine.exports.arn
}
