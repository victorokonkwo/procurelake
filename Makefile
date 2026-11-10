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
           infrastructure/modules/iam infrastructure/bootstrap infrastructure/live/dev/foundation

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

.PHONY: test-unit
test-unit: ## Unit tests (no services required)
	uv run pytest tests/unit

.PHONY: test-db
test-db: check-env ## PostgreSQL schema and migration tests (requires make db-up)
	uv run pytest -m postgres

.PHONY: test
test: check-env ## All tests (requires make db-up)
	uv run pytest
