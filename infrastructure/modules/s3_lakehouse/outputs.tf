output "bucket_names" {
  description = "Bucket names keyed by role (raw, lake, products, artifacts)."
  value       = { for k, b in aws_s3_bucket.this : k => b.bucket }
}

output "bucket_arns" {
  description = "Bucket ARNs keyed by role (raw, lake, products, artifacts)."
  value       = { for k, b in aws_s3_bucket.this : k => b.arn }
}
