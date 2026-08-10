PYTHON := .venv/bin/python
PIP := .venv/bin/pip
UV := uv

.PHONY: setup db-up db-down migrate seed mock-run openai-run serve test lint typecheck check demo-history clean db-backup db-list-backups db-restore db-verify-backup

## Bootstrap the environment (venv + install in editable mode with dev deps).
setup:
	$(UV) venv --python 3.12 .venv
	$(UV) pip install -e ".[dev]"

## Start the local PostgreSQL container.
db-up:
	docker compose up -d db

db-down:
	docker compose down

## Apply Alembic migrations.
migrate:
	$(PYTHON) -m endpoint_ledger.cli db upgrade

## Seed endpoint configurations and probe definitions (idempotent).
seed:
	$(PYTHON) -m endpoint_ledger.cli seed

## Run a full mock smoke suite (no live APIs).
mock-run:
	$(PYTHON) -m endpoint_ledger.cli run-suite --suite smoke --provider mock

## Run a live OpenAI smoke suite. Requires OPENAI_API_KEY and --live.
openai-run:
	$(PYTHON) -m endpoint_ledger.cli run-suite --suite smoke --provider openai --live

## Start the dashboard.
serve:
	$(PYTHON) -m endpoint_ledger.cli serve

## Generate demo history and a change event (stable -> persistent regression).
demo-history:
	$(PYTHON) -m endpoint_ledger.cli generate-demo-history --execute

## Create a timestamped compressed PostgreSQL backup.
db-backup:
	$(PYTHON) -m endpoint_ledger.backup create

## List available backups.
db-list-backups:
	$(PYTHON) -m endpoint_ledger.backup list

## Restore a backup into the configured database (make db-restore BACKUP=<path>).
db-restore:
	$(PYTHON) -m endpoint_ledger.backup restore "$(BACKUP)"

## Verify a backup by restoring into a temporary database (make db-verify-backup BACKUP=<path>).
db-verify-backup:
	$(PYTHON) -m endpoint_ledger.backup verify "$(BACKUP)"

test:
	$(PYTHON) -m pytest

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy endpoint_ledger

## All non-live validation: lint + typecheck + tests.
check: lint typecheck test

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist *.egg-info
