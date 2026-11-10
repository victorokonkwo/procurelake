# Customer-managed KMS key with automatic rotation.
#
# No explicit key policy is set, so AWS applies its default key policy: the account root
# principal gets full access, which delegates authorisation to IAM policies. Each role
# that uses the key must be granted kms:* actions on it explicitly (see modules/iam).

resource "aws_kms_key" "this" {
  description             = var.description
  enable_key_rotation     = true
  deletion_window_in_days = var.deletion_window_in_days
  tags                    = var.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.alias}"
  target_key_id = aws_kms_key.this.key_id
}
