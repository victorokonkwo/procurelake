terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.72.0, < 7.0.0"
    }
  }

  # Partial configuration: bucket, region, kms_key_id and use_lockfile come from the
  # git-ignored backend.hcl (printed by `terraform output -raw backend_hcl` in bootstrap).
  backend "s3" {
    key = "dev/data_platform/terraform.tfstate"
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project     = "procurelake"
      Environment = "dev"
      Stack       = "data_platform"
      ManagedBy   = "terraform"
      Repository  = "victorokonkwo/procurelake"
    }
  }
}
