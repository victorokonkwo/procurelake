mock_provider "aws" {
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:ca-central-1:123456789012:repository/procurelake-dev-dbt-runner"
      repository_url = "123456789012.dkr.ecr.ca-central-1.amazonaws.com/procurelake-dev-dbt-runner"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/procurelake-dev-dbt-runner-execution" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ca-central-1:123456789012:log-group:/procurelake/procurelake-dev-dbt-runner" }
  }
}

variables {
  name           = "procurelake-dev-dbt-runner"
  aws_account_id = "123456789012"
  region         = "ca-central-1"
  vpc_id         = "vpc-1"
  kms_key_arn    = "arn:aws:kms:ca-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
  task_role_arn  = "arn:aws:iam::123456789012:role/procurelake-dev-data-engineer"
}

run "no_task_definition_without_an_image" {
  command = apply

  assert {
    condition     = length(aws_ecs_task_definition.this) == 0 && output.task_definition_arn == null
    error_message = "The task definition waits for a reviewed image digest."
  }
  assert {
    condition     = aws_ecr_repository.this.image_tag_mutability == "IMMUTABLE" && aws_ecr_repository.this.image_scanning_configuration[0].scan_on_push
    error_message = "Images must be immutable and scanned."
  }
}

run "task_runs_the_image_by_digest_on_arm64" {
  command = apply

  variables {
    image_digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
  }

  assert {
    condition     = jsondecode(output.container_definitions_for_test)[0].image == "123456789012.dkr.ecr.ca-central-1.amazonaws.com/procurelake-dev-dbt-runner@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    error_message = "The task must reference the image by digest."
  }
  assert {
    condition     = aws_ecs_task_definition.this[0].runtime_platform[0].cpu_architecture == "ARM64" && aws_ecs_task_definition.this[0].task_role_arn == var.task_role_arn
    error_message = "ARM64, running as the given task role."
  }
}

run "task_has_no_ingress" {
  command = apply

  assert {
    condition     = aws_vpc_security_group_egress_rule.https.from_port == 443 && aws_vpc_security_group_egress_rule.https.to_port == 443
    error_message = "Egress is HTTPS only."
  }
}

run "rejects_a_tag_instead_of_a_digest" {
  command = plan

  variables {
    image_digest = "latest"
  }
  expect_failures = [var.image_digest]
}
