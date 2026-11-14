output "key_arn" {
  description = "ARN of the KMS key."
  value       = aws_kms_key.this.arn
}

output "key_id" {
  description = "ID of the KMS key."
  value       = aws_kms_key.this.key_id
}

output "policy_for_test" {
  description = "Key policy JSON (asserted by root modules' offline tests)."
  value       = aws_kms_key.this.policy
}

output "alias_name" {
  description = "Full alias name (alias/...)."
  value       = aws_kms_alias.this.name
}
