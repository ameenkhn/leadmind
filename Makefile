.DEFAULT_GOAL := help
PY := .venv/bin/python
VENV := .venv

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/python:
	uv venv --python 3.11 $(VENV)

.PHONY: install
install: $(VENV)/bin/python ## Create the virtualenv and install dependencies
	uv pip install -e ".[dev]"

.PHONY: db-up
db-up: ## Start PostgreSQL 16 + pgvector
	docker compose up -d db
	@until docker compose exec -T db pg_isready -U leadmind -d leadmind >/dev/null 2>&1; do sleep 1; done
	@echo "database ready"

.PHONY: db-down
db-down: ## Stop the database (data is preserved in the named volume)
	docker compose down

.PHONY: db-reset
db-reset: ## Destroy the database volume and rebuild the schema from scratch
	docker compose down -v
	$(MAKE) db-up
	$(MAKE) migrate

.PHONY: migrate
migrate: ## Apply all migrations
	$(VENV)/bin/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add foo"
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: ingest
ingest: ## Ingest the source workbook
	$(VENV)/bin/leadmind ingest data/raw/Outbound_Leads.xlsx

.PHONY: ingest-dry
ingest-dry: ## Process the workbook without writing anything
	$(VENV)/bin/leadmind ingest data/raw/Outbound_Leads.xlsx --dry-run

.PHONY: test
test: ## Run the whole suite (needs a running database)
	$(VENV)/bin/pytest backend/tests -q

.PHONY: test-unit
test-unit: ## Run unit tests only (no database required)
	$(VENV)/bin/pytest backend/tests/unit -q

.PHONY: lint
lint: ## Lint and type-check
	$(VENV)/bin/ruff check backend/
	$(VENV)/bin/ruff format --check backend/
	$(VENV)/bin/mypy

.PHONY: fmt
fmt: ## Auto-format and auto-fix
	$(VENV)/bin/ruff format backend/
	$(VENV)/bin/ruff check backend/ --fix

.PHONY: check
check: lint test ## Everything CI runs

.PHONY: profile
profile: ## Re-run the dataset profiling scripts used for docs/01-dataset-analysis.md
	@for f in scripts/profiling/*.py; do echo "--- $$f"; $(PY) $$f; done
