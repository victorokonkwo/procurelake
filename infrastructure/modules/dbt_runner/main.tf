# dbt runner on ECS Fargate (ADR 0029; open question 16 resolved).
#
#   - ECR repository: immutable tags, scan on push, keep the last 10 images.
#   - Task definition (ARM64, 0.5 vCPU / 1 GiB), created once an image digest is given: it runs
#     the image by digest, so a task always runs exactly the image that was reviewed.
#   - Execution role: pull from this repository and write this log group only.
#   - Task role: passed in (the data-engineer role: dbt runs with the same rights in ECS as from
#     a laptop). Security group: no ingress; HTTPS egress only (AWS APIs).

data "aws_partition" "current" {}

resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/procurelake/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_security_group" "task" {
  # checkov:skip=CKV2_AWS_5: false positive: attached to each dbt Fargate task at RunTask (state machine NetworkConfiguration)
  name        = "${var.name}-task"
  description = "dbt runner tasks: no ingress; HTTPS egress to AWS APIs."
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS to AWS APIs (Athena, Glue, S3, ECR, CloudWatch Logs, STS)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_iam_role" "execution" {
  name        = "${var.name}-execution"
  description = "ECS task execution role for ${var.name}: image pull and logs only."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "execution" {
  name = "pull-and-log"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EcrAuth", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid      = "PullThisRepository"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.this.arn
      },
      { Sid = "DecryptImageLayers", Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn },
      {
        Sid      = "WriteThisLogGroup"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.this.arn}:*"
      },
    ]
  })
}

resource "aws_ecs_task_definition" "this" {
  count = var.image_digest == "" ? 0 : 1

  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "dbt"
    image     = "${aws_ecr_repository.this.repository_url}@${var.image_digest}"
    essential = true
    environment = [
      for k, v in var.environment : { name = k, value = v }
    ]
    readonlyRootFilesystem = false
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "dbt"
      }
    }
  }])

  tags = var.tags
}
