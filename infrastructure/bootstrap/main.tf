# Bootstrap stack: applied once, by a human, with local state.
#
# Creates, in this order of importance:
#   1. The AWS cost budget and alerts. The plan requires these before any persistent
#      infrastructure exists.
#   2. The remote-state bucket (versioned, KMS-encrypted, TLS-only, native S3 locking) used by
#      every root module under infrastructure/live/.
#   3. GitHub Actions OIDC federation with a read-only plan role and a dev deploy role
#      (ADR 0010: no long-lived AWS keys in CI).
#
# See infrastructure/README.md for the apply procedure and how to move this stack's own state
# into the bucket afterwards.

terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.72.0, < 7.0.0"
    }
  }

  # First apply ran with local state. The state was then migrated into the bucket this stack
  # created (infrastructure/README.md, step 4). Use the same git-ignored backend.hcl as the
  # live roots: terraform init -backend-config=../live/dev/foundation/backend.hcl
  backend "s3" {
    key = "bootstrap/terraform.tfstate"
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project    = "procurelake"
      Stack      = "bootstrap"
      ManagedBy  = "terraform"
      Repository = var.github_repository
    }
  }
}

locals {
  state_bucket_name = "procurelake-tfstate-${var.aws_account_id}-${var.region}"
  github_oidc_host  = "token.actions.githubusercontent.com"
  repo_subject      = "repo:${var.github_repository}"
}

# ----------------------------------------------------------------------------- 1. cost budget
module "budget" {
  source = "../modules/budgets"

  name              = "procurelake-monthly-cost"
  monthly_limit_usd = var.monthly_budget_usd
  alert_emails      = var.budget_alert_emails
}

# ----------------------------------------------------------------------------- 2. remote state
module "state_key" {
  source = "../modules/kms"

  description = "ProcureLake Terraform state encryption"
  alias       = "procurelake-tfstate"
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = module.state_key.key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "retain-state-history"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.state_noncurrent_version_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

# ----------------------------------------------------------------------------- 3. GitHub OIDC
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  state_access_statements = [
    {
      Sid      = "ListStateBucket"
      Effect   = "Allow"
      Action   = "s3:ListBucket"
      Resource = aws_s3_bucket.state.arn
    },
    {
      # Read state; write/delete *.tflock objects for native S3 state locking.
      Sid      = "ReadWriteStateAndLocks"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = "${aws_s3_bucket.state.arn}/*"
    },
    {
      Sid      = "UseStateKey"
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
      Resource = module.state_key.key_arn
    },
  ]
}

# Plan role: pull requests and pushes to the integration branch. Read-only on AWS, plus state access.
resource "aws_iam_role" "github_plan" {
  name                 = "procurelake-github-plan"
  description          = "GitHub Actions: terraform plan (read-only) for ${var.github_repository}"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "${local.github_oidc_host}:aud" = "sts.amazonaws.com" }
        StringLike = {
          "${local.github_oidc_host}:sub" = [
            "${local.repo_subject}:pull_request",
            "${local.repo_subject}:ref:refs/heads/${var.github_integration_branch}",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "github_plan_readonly" {
  role       = aws_iam_role.github_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "github_plan" {
  name = "terraform-plan"
  role = aws_iam_role.github_plan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.state_access_statements, [
      {
        # ReadOnlyAccess includes s3:GetObject everywhere. CI must never read lake data. The only
        # exceptions are state and the deployed Glue code (scripts and wheels under glue/ in the
        # artifacts bucket), which terraform plan must read to refresh those aws_s3_object
        # resources. That prefix holds code built from this public repository, never data.
        Sid    = "DenyReadingPlatformData"
        Effect = "Deny"
        Action = ["s3:GetObject", "s3:GetObjectVersion"]
        NotResource = [
          "${aws_s3_bucket.state.arn}/*",
          "arn:aws:s3:::procurelake-dev-artifacts-${var.aws_account_id}-${var.region}/glue/*",
        ]
      },
    ])
  })
}

# Deploy role: only workflows running in the GitHub "dev" environment (which can require approval).
resource "aws_iam_role" "github_deploy_dev" {
  name                 = "procurelake-github-deploy-dev"
  description          = "GitHub Actions: terraform apply for the dev environment of ${var.github_repository}"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.github_oidc_host}:aud" = "sts.amazonaws.com"
          "${local.github_oidc_host}:sub" = "${local.repo_subject}:environment:dev"
        }
      }
    }]
  })
}

# KNOWN GAP (docs/open-questions.md): AdministratorAccess is broader than least privilege.
# It is limited to the dev environment trust above. Replace it with a scoped policy once the
# full dev resource set is known (Week 10 security review).
resource "aws_iam_role_policy_attachment" "github_deploy_dev_admin" {
  role       = aws_iam_role.github_deploy_dev.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
