output "state_bucket_name" {
  description = "Remote-state bucket for all infrastructure/live root modules."
  value       = aws_s3_bucket.state.bucket
}

output "state_kms_key_arn" {
  description = "KMS key encrypting Terraform state."
  value       = module.state_key.key_arn
}

output "github_plan_role_arn" {
  description = "Role assumed by GitHub Actions for terraform plan."
  value       = aws_iam_role.github_plan.arn
}

output "github_deploy_dev_role_arn" {
  description = "Role assumed by GitHub Actions (environment: dev) for terraform apply."
  value       = aws_iam_role.github_deploy_dev.arn
}

output "backend_hcl" {
  description = "Contents for infrastructure/live/<env>/<area>/backend.hcl (git-ignored)."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    region       = "${var.region}"
    encrypt      = true
    kms_key_id   = "${module.state_key.key_arn}"
    use_lockfile = true
  EOT
}
