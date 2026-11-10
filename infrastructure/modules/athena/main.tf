# Athena workgroup for engineering queries.
#
# The workgroup configuration is enforced, so clients cannot override the result location or
# encryption. Results go to the artifacts bucket under athena_results/ (expired after 30 days
# by the s3_lakehouse lifecycle), SSE-KMS with the platform key. A per-query scan cutoff caps
# the cost of any single query.

resource "aws_athena_workgroup" "this" {
  name          = var.name
  description   = var.description
  state         = "ENABLED"
  force_destroy = false

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.bytes_scanned_cutoff_per_query
    requester_pays_enabled             = false

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }

    result_configuration {
      output_location = "s3://${var.results_bucket_name}/athena_results/${var.name}/"

      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = var.kms_key_arn
      }
    }
  }
}

resource "aws_athena_named_query" "this" {
  for_each = var.named_queries

  name        = each.key
  description = each.value.description
  workgroup   = aws_athena_workgroup.this.id
  database    = each.value.database
  query       = each.value.query
}
