output "job_name" {
  description = "Glue job name."
  value       = aws_glue_job.this.name
}

output "role_arn" {
  description = "Job execution role ARN."
  value       = aws_iam_role.job.arn
}

output "role_name" {
  description = "Job execution role name."
  value       = aws_iam_role.job.name
}

output "wheel_uri" {
  description = "S3 URI of the deployed wheel."
  value       = "s3://${var.artifacts_bucket_name}/${aws_s3_object.wheel.key}"
}

output "wheel_sha256" {
  description = "SHA-256 of the deployed wheel."
  value       = local.wheel_sha256
}

output "job_arguments_for_test" {
  description = "Effective default arguments (asserted by the root module's offline tests)."
  value       = aws_glue_job.this.default_arguments
}
