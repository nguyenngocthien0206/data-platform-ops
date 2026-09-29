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

PYTEST_ARGS ?=
# Legacy engines the docker-* targets start. CI starts Postgres only and runs
# SQL Server in its own on-demand job: make docker-test DOCKER_ENGINES=postgres
DOCKER_ENGINES ?= postgres sqlserver

.PHONY: help setup up down seed build simulate cost incidents reconcile \
        dashboard readme-check test lint fmt clean pipeline demo check-env \
        docker-build docker-up docker-demo docker-test docker-lint \
        docker-metadata-check docker-readme-check docker-dashboard \
        docker-browser-check docker-shell docker-clean

help: ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python dependencies and dbt packages
	$(UV) sync
	@if [ -f dbt/packages.yml ]; then $(RUN) dbt deps --project-dir dbt; \
	else echo "no dbt/packages.yml yet, skipping dbt deps (phase 1)"; fi

up: ## Start Docker services (postgres)
	@if [ ! -f .env ]; then \
		echo "error: .env is missing. Run: cp .env.example .env"; exit 1; fi
	$(COMPOSE) up -d --wait postgres

down: ## Stop Docker services
	$(COMPOSE) --profile sqlserver --profile app --profile dashboard --profile browser down

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

dashboard: ## Launch the Streamlit dashboards (read-only over the ops schema)
	$(RUN) platform-ops dashboard

# Not part of `demo`: the README numbers come from the reference run, and a
# first `make demo` on another machine should not fail on a documentation check.
readme-check: ## Check every README results number against reports/ (after make demo)
	$(RUN) python scripts/check_readme.py

test: ## Run the unit and integration tests (extra flags in PYTEST_ARGS)
	$(RUN) pytest $(PYTEST_ARGS)

# mypy runs as a module rather than through the generated mypy.exe shim, because
# some managed Windows machines refuse to launch unsigned shim executables.
lint: ## Lint, check formatting, and type check
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) python -m mypy

fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

# Empties the generated directories rather than removing them: inside the
# container they are volume mount points, which cannot be removed.
clean: ## Remove generated data, reports and build artifacts
	rm -rf data/*.duckdb data/*.duckdb.wal
	find warehouse dbt/target dbt/logs -mindepth 1 -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find reports -type f ! -name .gitkeep -delete 2>/dev/null || true

# `simulate` re-seeds and builds on its first simulated day, so `demo` needs no
# separate `seed` and `build` steps.
pipeline: simulate cost incidents reconcile ## Every module in order, on the current state
	@echo "pipeline complete. Reports are in reports/."

demo: clean setup up pipeline ## Full end to end run on a clean state

# -- in the container ------------------------------------------------------------
#
# The same work inside the toolkit image (Dockerfile), next to Postgres and SQL
# Server in Compose. No Python, uv or make on the host beyond this Makefile.
# Code changes need `make docker-build`; generated state lives on volumes.

check-env:
	@if [ ! -f .env ]; then \
		echo "error: .env is missing. Run: cp .env.example .env"; exit 1; fi

docker-build: check-env ## Build the toolkit image
	$(COMPOSE) build app

docker-up: check-env ## Start the legacy engines for the container (DOCKER_ENGINES)
	$(COMPOSE) --profile sqlserver up -d --wait $(DOCKER_ENGINES)

docker-demo: docker-up ## Full end to end run in the container on a clean state
	$(COMPOSE) run --rm app make clean pipeline

docker-test: docker-up ## Run the tests in the container, skips listed with their reason
	$(COMPOSE) run --rm app make test PYTEST_ARGS=-rs

docker-lint: check-env ## Lint, check formatting, and type check in the container
	$(COMPOSE) run --rm app make lint

# CODEOWNERS for data: every model, source and exposure has exactly one owner.
# --parse builds the manifest without any data.
docker-metadata-check: check-env ## Check dataset ownership in the container
	$(COMPOSE) run --rm app platform-ops metadata check --parse

docker-readme-check: check-env ## Check README numbers against reports/ in the container
	$(COMPOSE) run --rm app make readme-check

docker-dashboard: check-env ## Serve the dashboards from the container on localhost:8501
	$(COMPOSE) --profile dashboard up dashboard

docker-browser-check: check-env ## Load every dashboard page in headless Chromium
	$(COMPOSE) --profile browser run --rm browser-check; \
	status=$$?; $(COMPOSE) --profile browser stop dashboard; exit $$status

docker-shell: check-env ## Open a shell in the toolkit container
	$(COMPOSE) run --rm app bash

docker-clean: check-env ## Remove the container's generated state and the reports
	$(COMPOSE) --profile app --profile dashboard --profile browser rm --force --stop
	docker volume rm --force $(addprefix data-platform-ops_,app_data app_warehouse app_dbt_target app_dbt_logs)
	find reports -type f ! -name .gitkeep -delete 2>/dev/null || true
