# Orchestration layer for data-platform-ops. Deliberately a Makefile plus a
# Python CLI rather than an orchestrator: the whole point is that one laptop,
# offline, can run the entire platform.
#
# Recipes run under bash so this file behaves the same on Linux, macOS, and on
# Windows with GNU make installed (winget install ezwinports.make).

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
RUN := $(UV) run
COMPOSE ?= docker compose

.PHONY: help setup up down seed build simulate cost incidents reconcile \
        dashboard test lint fmt clean demo

help: ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python dependencies and dbt packages
	$(UV) sync
	@if [ -f dbt/packages.yml ]; then $(RUN) dbt deps --project-dir dbt; \
	else echo "no dbt/packages.yml yet, skipping dbt deps (phase 1)"; fi

up: ## Start Docker services (postgres)
	@if [ ! -f .env ]; then \
		echo "error: .env is missing. Run: cp .env.example .env"; exit 1; fi
	$(COMPOSE) up -d --wait postgres

down: ## Stop Docker services
	$(COMPOSE) --profile sqlserver down

seed: ## Generate raw data for the simulated company
	$(RUN) platform-ops seed

build: ## Run dbt build
	$(RUN) platform-ops build

simulate: ## Run the workload generator over the simulated window
	$(RUN) platform-ops simulation run

cost: ## Collect, price, attribute, write the cost report
	$(RUN) platform-ops cost report

incidents: ## Inject faults, detect and group incidents, write metrics
	$(RUN) platform-ops incidents run

reconcile: ## Run the migration scenario and the diff, write the sign-off report
	$(RUN) platform-ops reconcile run

dashboard: ## Launch streamlit
	$(RUN) platform-ops dashboard

test: ## Run the unit and integration tests
	$(RUN) pytest

# mypy runs as a module rather than through the generated mypy.exe shim, because
# some managed Windows machines refuse to launch unsigned shim executables.
lint: ## Lint, check formatting, and type check
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) python -m mypy

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

clean: ## Remove generated data, reports and build artifacts
	rm -rf data/*.duckdb data/*.duckdb.wal warehouse dbt/target dbt/logs
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find reports -type f ! -name .gitkeep -delete 2>/dev/null || true

# `simulate` re-seeds and builds on its first simulated day, so `demo` needs no
# separate `seed` and `build` steps.
demo: clean setup up simulate cost incidents reconcile ## Full end to end run
	@echo "demo complete. Reports are in reports/."
