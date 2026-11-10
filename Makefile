SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load local settings (git-ignored). Copy .env.example to .env first.
-include .env
export

COMPOSE := docker compose --env-file .env -f docker/docker-compose.yml

.PHONY: help
help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

.PHONY: check-env
check-env:
	@test -f .env || { echo "Missing .env. Run: cp .env.example .env"; exit 1; }

.PHONY: install
install: ## Create the virtualenv and install locked dependencies
	uv sync --locked

.PHONY: db-up
db-up: check-env ## Start local PostgreSQL and wait until healthy
	$(COMPOSE) up -d --wait postgres

.PHONY: db-down
db-down: check-env ## Stop local PostgreSQL (data volume is kept)
	$(COMPOSE) down

.PHONY: db-reset
db-reset: check-env ## Stop PostgreSQL AND delete its local data volume
	@read -p "Delete the local PostgreSQL data volume? [y/N] " ans && [ "$$ans" = "y" ]
	$(COMPOSE) down -v

.PHONY: db-migrate
db-migrate: check-env ## Apply pending operational-schema migrations
	uv run python scripts/local/db_migrate.py

.PHONY: db-status
db-status: check-env ## Show applied and pending migrations
	uv run python scripts/local/db_migrate.py --status

SEED ?= 42
SCALE ?= 1

.PHONY: seed-preview
seed-preview: ## Generate the synthetic dataset without loading (counts + fingerprint)
	uv run python scripts/local/seed_db.py --dry-run --seed $(SEED) --scale $(SCALE)

.PHONY: db-seed
db-seed: check-env ## Load the synthetic dataset into an empty local database (SEED=42 SCALE=1)
	uv run python scripts/local/seed_db.py --seed $(SEED) --scale $(SCALE)

.PHONY: db-reseed
db-reseed: check-env ## Truncate all procurement tables, then load the synthetic dataset
	uv run python scripts/local/seed_db.py --reset --seed $(SEED) --scale $(SCALE)

.PHONY: db-psql
db-psql: check-env ## Open psql inside the PostgreSQL container
	$(COMPOSE) exec postgres psql -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"

.PHONY: lint
lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

.PHONY: format
format: ## Apply Ruff formatting and autofixes
	uv run ruff format .
	uv run ruff check --fix .

TF_DIRS := infrastructure/modules/kms infrastructure/modules/budgets infrastructure/modules/s3_lakehouse \
           infrastructure/modules/iam infrastructure/modules/glue_catalog infrastructure/modules/athena \
           infrastructure/modules/glue_spark_job infrastructure/modules/dms_account_roles \
           infrastructure/modules/cdc_network infrastructure/modules/rds_postgresql \
           infrastructure/modules/dms_s3_cdc infrastructure/live/dev/cdc \
           infrastructure/modules/public_network infrastructure/modules/dbt_runner \
           infrastructure/live/dev/security \
           infrastructure/bootstrap infrastructure/live/dev/foundation infrastructure/live/dev/data_platform

.PHONY: tf-fmt
tf-fmt: ## terraform fmt check (recursive)
	terraform fmt -check -recursive infrastructure

.PHONY: tf-validate
tf-validate: ## terraform init (no backend) + validate for every module and root (offline)
	@set -e; for d in $(TF_DIRS); do echo "== $$d"; terraform -chdir=$$d init -backend=false -input=false >/dev/null; terraform -chdir=$$d validate; done

.PHONY: tf-test
tf-test: tf-validate ## terraform test with a mock AWS provider (offline, creates nothing)
	@set -e; for d in $(TF_DIRS); do echo "== $$d"; terraform -chdir=$$d test; done

.PHONY: bootstrap-plan
bootstrap-plan: ## Plan the bootstrap stack (needs AWS credentials + terraform.tfvars)
	terraform -chdir=infrastructure/bootstrap init -input=false
	terraform -chdir=infrastructure/bootstrap plan -input=false -out=bootstrap.tfplan

.PHONY: dev-foundation-plan
dev-foundation-plan: ## Plan dev/foundation (needs AWS credentials, backend.hcl, terraform.tfvars)
	terraform -chdir=infrastructure/live/dev/foundation init -input=false -backend-config=backend.hcl
	terraform -chdir=infrastructure/live/dev/foundation plan -input=false -out=foundation.tfplan

.PHONY: glue-wheel
glue-wheel: ## Build the procurelake wheel deployed to Glue (reproducible; dist/ holds only this wheel)
	rm -rf dist
	uv build --wheel --out-dir dist

DBT_DIGEST_FILE := build/dbt_runner.digest

.PHONY: dev-data-platform-plan
dev-data-platform-plan: glue-wheel ## Plan dev/data_platform (Glue jobs, orchestration; uses build/dbt_runner.digest if present)
	terraform -chdir=infrastructure/live/dev/data_platform init -input=false -backend-config=backend.hcl
	terraform -chdir=infrastructure/live/dev/data_platform plan -input=false -out=data_platform.tfplan \
	  -var dbt_image_digest=$$(cat $(DBT_DIGEST_FILE) 2>/dev/null)

.PHONY: dbt-image
dbt-image: ## Build the dbt runner image (ARM64), push it to ECR, record its digest in build/dbt_runner.digest
	@REPO=$$(terraform -chdir=infrastructure/live/dev/data_platform output -raw dbt_runner_repository_url) && \
	  REGISTRY=$${REPO%%/*} && \
	  docker build --platform linux/arm64 -f jobs/containers/dbt_runner/Dockerfile -t procurelake-dbt-runner:local . && \
	  TAG=img-$$(docker image inspect procurelake-dbt-runner:local --format '{{.Id}}' | cut -c8-19) && \
	  aws ecr get-login-password --region ca-central-1 | docker login --username AWS --password-stdin $$REGISTRY && \
	  docker tag procurelake-dbt-runner:local $$REPO:$$TAG && \
	  docker push $$REPO:$$TAG && \
	  mkdir -p build && \
	  docker image inspect $$REPO:$$TAG --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep "^$$REPO@" | head -1 | cut -d@ -f2 > $(DBT_DIGEST_FILE) && \
	  echo "pushed $$REPO:$$TAG digest $$(cat $(DBT_DIGEST_FILE))"

.PHONY: orchestration-run
orchestration-run: ## Start cdc_processing once by hand and wait (dev/cdc must be up)
	uv run python scripts/aws/run_state_machine.py \
	  $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw cdc_processing_state_machine_arn) \
	  --input "{\"dms_task_arn\": \"$$($(CDCOUT) -raw replication_task_arn)\"}"

.PHONY: tenant-exports-run
tenant-exports-run: ## Run the daily tenant Parquet exports once by hand and wait (ADR 0030)
	uv run python scripts/aws/run_state_machine.py \
	  $$(terraform -chdir=infrastructure/live/dev/security output -raw tenant_exports_state_machine_arn) \
	  --name manual-$$(date -u +%Y%m%dT%H%M%SZ)

.PHONY: silver-run
silver-run: ## Run the Bronze->Silver Glue job on the latest complete snapshot batch and wait (BATCH= to pick one)
	uv run python scripts/aws/run_glue_job.py \
	  $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw bronze_to_silver_job_name) \
	  $(if $(BATCH),--arg=--batch-id=$(BATCH),)

DEV_TF := infrastructure/live/dev/foundation

.PHONY: ingest-usaspending
ingest-usaspending: ## Ingest one settled day from USAspending into dev Bronze as the ingestion role (DATE=YYYY-MM-DD)
	@test -n "$(DATE)" || { echo "Usage: make ingest-usaspending DATE=YYYY-MM-DD"; exit 1; }
	PROCURELAKE_GIT_SHA=$$(git rev-parse --short HEAD) uv run python -m procurelake.ingestion.cli usaspending \
	  --date $(DATE) \
	  --raw-bucket $$(terraform -chdir=$(DEV_TF) output -json bucket_names | uv run python -c "import sys,json;print(json.load(sys.stdin)['raw'])") \
	  --manifest-bucket $$(terraform -chdir=$(DEV_TF) output -json bucket_names | uv run python -c "import sys,json;print(json.load(sys.stdin)['artifacts'])") \
	  --role-arn $$(terraform -chdir=$(DEV_TF) output -raw ingestion_role_arn)

.PHONY: ingest-usaspending-local
ingest-usaspending-local: ## Ingest one settled day into ./local_lake (no AWS) (DATE=YYYY-MM-DD)
	@test -n "$(DATE)" || { echo "Usage: make ingest-usaspending-local DATE=YYYY-MM-DD"; exit 1; }
	uv run python -m procurelake.ingestion.cli usaspending --date $(DATE) --local-root local_lake

TFOUT = terraform -chdir=$(DEV_TF) output
BUCKET = $$($(TFOUT) -json bucket_names | uv run python -c "import sys,json;print(json.load(sys.stdin)['$(1)'])")

# dbt Gold (ADR 0026): runs as the data-engineer role with temporary credentials.
DBT_DIRS = --project-dir dbt --profiles-dir dbt

TERRAVISION := uvx --from terravision==0.56.1 terravision
DIAGRAMS_DIR := docs/architecture/diagrams

.PHONY: diagrams
diagrams: ## Render the dev architecture diagram (TerraVision; needs Graphviz, no AWS)
	@cd $(DIAGRAMS_DIR) && for f in png svg; do \
	  $(TERRAVISION) draw --source dev-platform.tvg.json --annotate dev-platform.terravision.yml \
	    --format $$f --outfile dev-platform >/dev/null && mv dev-platform.dot.$$f dev-platform.$$f; done
	@echo "Wrote $(DIAGRAMS_DIR)/dev-platform.png and .svg"

.PHONY: dbt-parse
dbt-parse: ## dbt parse (offline; no AWS access)
	PROCURELAKE_DBT_S3_STAGING_DIR=s3://example-bucket/athena_results/ \
	PROCURELAKE_DBT_S3_DATA_DIR=s3://example-bucket/gold/ \
	uv run --group dbt dbt parse $(DBT_DIRS)

.PHONY: dbt-build
dbt-build: ## dbt build (Gold models + tests) on dev Athena as the data-engineer role
	PROCURELAKE_DBT_S3_STAGING_DIR=s3://$(call BUCKET,artifacts)/athena_results/ \
	PROCURELAKE_DBT_S3_DATA_DIR=s3://$(call BUCKET,lake)/gold/ \
	PROCURELAKE_GOLD_SCHEMA=$$($(TFOUT) -raw gold_database_name) \
	PROCURELAKE_SILVER_SCHEMA=$$($(TFOUT) -raw silver_database_name) \
	PROCURELAKE_ATHENA_WORKGROUP=$$($(TFOUT) -raw athena_workgroup_name) \
	uv run --group dbt python scripts/aws/with_role.py $$($(TFOUT) -raw data_engineer_role_arn) -- \
	  uv run --group dbt dbt build $(DBT_DIRS)

# CDC (ADR 0027): disposable, hourly-billed stack. Destroy it after every session (cdc-down).
CDC_TF := infrastructure/live/dev/cdc
CDCOUT = terraform -chdir=$(CDC_TF) output

.PHONY: cdc-plan
cdc-plan: ## Plan dev/cdc phase 1 (network, RDS, secrets); PHASE=2 adds DMS (needs backend.hcl, terraform.tfvars)
	terraform -chdir=$(CDC_TF) init -input=false -backend-config=backend.hcl
	terraform -chdir=$(CDC_TF) plan -input=false -var enable_dms=$(if $(filter 2,$(PHASE)),true,false) -out=cdc.tfplan

.PHONY: cdc-source-setup
cdc-source-setup: ## Migrate + seed the RDS source and create the DMS user (after phase 1 apply)
	uv run python scripts/aws/cdc_source_setup.py \
	  --host $$($(CDCOUT) -raw source_address) --port $$($(CDCOUT) -raw source_port) \
	  --database $$($(CDCOUT) -raw source_database_name) \
	  --master-secret-arn $$($(CDCOUT) -raw master_user_secret_arn) \
	  --replication-secret-arn $$($(CDCOUT) -raw replication_secret_arn)

.PHONY: cdc-workload
cdc-workload: ## Apply the CDC workload to the RDS source (SEED=7 OPS=40); changes recorded in build/evidence/
	uv run python scripts/aws/cdc_workload.py \
	  --host $$($(CDCOUT) -raw source_address) --port $$($(CDCOUT) -raw source_port) \
	  --database $$($(CDCOUT) -raw source_database_name) \
	  --master-secret-arn $$($(CDCOUT) -raw master_user_secret_arn) \
	  --seed $(or $(SEED_WL),7) --operations $(or $(OPS),40)

.PHONY: cdc-apply
cdc-apply: ## Run the CDC apply Glue job (Bronze DMS files -> Silver) and wait; uses the DMS task if dev/cdc is up
	uv run python scripts/aws/run_glue_job.py \
	  $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw apply_cdc_job_name) \
	  $$(T=$$($(CDCOUT) -raw replication_task_arn 2>/dev/null); [ "$${T#arn:}" != "$$T" ] && echo "--arg=--dms-task-arn=$$T")

.PHONY: cdc-freshness
cdc-freshness: ## C5 gate, live: workload -> DMS -> CDC apply -> dbt build; per-change checks + latency p95
	uv run python scripts/aws/cdc_freshness.py \
	  --host $$($(CDCOUT) -raw source_address) --port $$($(CDCOUT) -raw source_port) \
	  --database $$($(CDCOUT) -raw source_database_name) \
	  --master-secret-arn $$($(CDCOUT) -raw master_user_secret_arn) \
	  --task-arn $$($(CDCOUT) -raw replication_task_arn) \
	  --glue-job $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw apply_cdc_job_name) \
	  --engineer-role-arn $$($(TFOUT) -raw data_engineer_role_arn) \
	  --workgroup $$($(TFOUT) -raw athena_workgroup_name) \
	  --silver-database $$($(TFOUT) -raw silver_database_name) \
	  --gold-database $$($(TFOUT) -raw gold_database_name) \
	  --seed $(or $(SEED_WL),11) --operations $(or $(OPS),40)

.PHONY: cdc-freshness-scheduled
cdc-freshness-scheduled: ## C5/Week 6, live: workload only; the 5-minute schedule runs the pipeline; poll until visible
	uv run python scripts/aws/cdc_freshness.py --scheduled \
	  --host $$($(CDCOUT) -raw source_address) --port $$($(CDCOUT) -raw source_port) \
	  --database $$($(CDCOUT) -raw source_database_name) \
	  --master-secret-arn $$($(CDCOUT) -raw master_user_secret_arn) \
	  --task-arn $$($(CDCOUT) -raw replication_task_arn) \
	  --glue-job $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw apply_cdc_job_name) \
	  --engineer-role-arn $$($(TFOUT) -raw data_engineer_role_arn) \
	  --workgroup $$($(TFOUT) -raw athena_workgroup_name) \
	  --silver-database $$($(TFOUT) -raw silver_database_name) \
	  --gold-database $$($(TFOUT) -raw gold_database_name) \
	  --seed $(or $(SEED_WL),31) --operations $(or $(OPS),40)

.PHONY: cdc-failure-drill
cdc-failure-drill: ## Week 6, live: hold the invoices lease, watch a scheduled run fail + alert, then recover
	uv run python scripts/aws/cdc_failure_drill.py \
	  --host $$($(CDCOUT) -raw source_address) --port $$($(CDCOUT) -raw source_port) \
	  --database $$($(CDCOUT) -raw source_database_name) \
	  --master-secret-arn $$($(CDCOUT) -raw master_user_secret_arn) \
	  --state-machine-arn $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw cdc_processing_state_machine_arn) \
	  --checkpoint-table $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw cdc_checkpoint_table_name) \
	  --alerts-topic-arn $$(terraform -chdir=infrastructure/live/dev/data_platform output -raw pipeline_alerts_topic_arn) \
	  --engineer-role-arn $$($(TFOUT) -raw data_engineer_role_arn) \
	  --workgroup $$($(TFOUT) -raw athena_workgroup_name) \
	  --silver-database $$($(TFOUT) -raw silver_database_name) \
	  --gold-database $$($(TFOUT) -raw gold_database_name)

.PHONY: cdc-down
cdc-down: ## Destroy the dev/cdc stack (asks for confirmation; Bronze data and state are kept)
	terraform -chdir=$(CDC_TF) destroy -input=true -var enable_dms=true

.PHONY: test-aws
test-aws: ## Integration tests against deployed dev AWS (needs AWS creds); evidence in build/evidence/
	PROCURELAKE_AWS_TESTS=1 \
	PROCURELAKE_DEV_RAW_BUCKET=$(call BUCKET,raw) \
	PROCURELAKE_DEV_ARTIFACTS_BUCKET=$(call BUCKET,artifacts) \
	PROCURELAKE_DEV_DATA_KMS_KEY_ARN=$$($(TFOUT) -raw data_kms_key_arn) \
	PROCURELAKE_DEV_INGESTION_ROLE_ARN=$$($(TFOUT) -raw ingestion_role_arn) \
	PROCURELAKE_DEV_DATA_ENGINEER_ROLE_ARN=$$($(TFOUT) -raw data_engineer_role_arn) \
	PROCURELAKE_DEV_ATHENA_WORKGROUP=$$($(TFOUT) -raw athena_workgroup_name) \
	PROCURELAKE_DEV_BRONZE_DATABASE=$$($(TFOUT) -raw bronze_database_name) \
	PROCURELAKE_DEV_SILVER_DATABASE=$$($(TFOUT) -raw silver_database_name) \
	PROCURELAKE_DEV_GOLD_DATABASE=$$($(TFOUT) -raw gold_database_name) \
	PROCURELAKE_DEV_CDC_CHECKPOINT_TABLE=$$(terraform -chdir=infrastructure/live/dev/data_platform output -raw cdc_checkpoint_table_name) \
	PROCURELAKE_DEV_TENANT_ROLE_ARNS="$$(terraform -chdir=infrastructure/live/dev/security output -json tenant_role_arns)" \
	PROCURELAKE_DEV_TENANT_WORKGROUPS="$$(terraform -chdir=infrastructure/live/dev/security output -json tenant_workgroups)" \
	PROCURELAKE_DEV_ADMIN_ROLE_ARN=$$(terraform -chdir=infrastructure/live/dev/security output -raw admin_role_arn) \
	PROCURELAKE_DEV_ADMIN_WORKGROUP=$$(terraform -chdir=infrastructure/live/dev/security output -raw admin_workgroup) \
	uv run pytest -m aws -v --junitxml=build/test-results/aws-integration.xml tests/integration/aws

.PHONY: test-unit
test-unit: ## Unit tests (no services required)
	uv run pytest tests/unit

.PHONY: test-db
test-db: check-env ## PostgreSQL schema and migration tests (requires make db-up)
	uv run pytest -m postgres

.PHONY: test
test: check-env ## All tests (requires make db-up)
	uv run pytest
