# Offline tests: mock AWS provider, no credentials, nothing is created.
mock_provider "aws" {}

variables {
  name_prefix    = "procurelake-dev"
  aws_account_id = "123456789012"
  region         = "ca-central-1"
  kms_key_arn    = "arn:aws:kms:ca-central-1:123456789012:key/11111111-2222-3333-4444-555555555555"
}

run "creates_the_four_documented_buckets" {
  command = apply

  assert {
    condition     = toset(keys(aws_s3_bucket.this)) == toset(["raw", "lake", "products", "artifacts"])
    error_message = "Expected exactly raw, lake, products and artifacts buckets."
  }
  assert {
    condition     = aws_s3_bucket.this["raw"].bucket == "procurelake-dev-raw-123456789012-ca-central-1"
    error_message = "Bucket naming must be <prefix>-<role>-<account>-<region>."
  }
  assert {
    condition     = alltrue([for b in aws_s3_bucket.this : length(b.bucket) <= 63 && b.force_destroy == false])
    error_message = "Bucket names must be <= 63 chars and force_destroy must be off."
  }
}

run "every_bucket_is_private_versioned_and_kms_encrypted" {
  command = apply

  assert {
    condition = alltrue([for p in aws_s3_bucket_public_access_block.this :
    p.block_public_acls && p.block_public_policy && p.ignore_public_acls && p.restrict_public_buckets])
    error_message = "All public access must be blocked on every bucket."
  }
  assert {
    condition     = alltrue([for o in aws_s3_bucket_ownership_controls.this : o.rule[0].object_ownership == "BucketOwnerEnforced"])
    error_message = "ACLs must be disabled (BucketOwnerEnforced)."
  }
  assert {
    condition     = alltrue([for v in aws_s3_bucket_versioning.this : v.versioning_configuration[0].status == "Enabled"])
    error_message = "Versioning must be enabled on every bucket."
  }
  assert {
    condition = alltrue([for e in aws_s3_bucket_server_side_encryption_configuration.this :
      one(e.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" &&
      one(e.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == var.kms_key_arn &&
    one(e.rule).bucket_key_enabled])
    error_message = "Every bucket must default to SSE-KMS with the platform key and bucket keys."
  }
}

run "every_bucket_denies_non_tls_requests" {
  command = apply

  assert {
    condition = alltrue([for p in aws_s3_bucket_policy.this :
      anytrue([for s in jsondecode(p.policy).Statement :
        s.Effect == "Deny" && s.Action == "s3:*" && s.Condition.Bool["aws:SecureTransport"] == "false"
    ])])
    error_message = "Every bucket policy must deny aws:SecureTransport=false."
  }
}

run "bronze_current_objects_never_expire" {
  command = apply

  assert {
    condition = alltrue([for r in aws_s3_bucket_lifecycle_configuration.this["raw"].rule :
    length(r.expiration) == 0])
    error_message = "Raw (Bronze) lifecycle must not expire current object versions."
  }
  assert {
    condition = anytrue([for r in aws_s3_bucket_lifecycle_configuration.this["artifacts"].rule :
    length(r.expiration) == 1 && r.expiration[0].days == 30 && r.filter[0].prefix == "athena_results/"])
    error_message = "Athena results under artifacts/athena_results/ must expire after 30 days."
  }
}

run "rejects_invalid_account_id" {
  command = plan

  variables {
    aws_account_id = "12345"
  }
  expect_failures = [var.aws_account_id]
}
