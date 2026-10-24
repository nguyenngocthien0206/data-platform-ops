# Orchestration layer for data-platform-ops. Deliberately a Makefile plus a
# Python CLI rather than an orchestrator: the whole point is that one laptop,
# offline, can run the entire platform.
#
# Recipes run under bash so this file behaves the same on Linux, macOS, and on
# Windows with GNU make installed (winget install ezwinports.make).
#
# On Windows that bash is Git for Windows' own. /usr/bin/env only exists inside
# Git Bash, so from PowerShell or cmd make would otherwise fall back to cmd.exe
# and fail on the first bash line; the bash.exe on PATH is often WSL's, which
# cannot see this checkout the same way. If Git lives elsewhere, pass its bash:
#   make GIT_BASH="D:/Tools/Git/bin/bash.exe" demo
ifeq ($(OS),Windows_NT)
GIT_BASH ?= C:/Program Files/Git/bin/bash.exe
SHELL := $(GIT_BASH)
else
SHELL := /usr/bin/env bash
endif
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
RUN := $(UV) run
COMPOSE ?= docker compose

PYTEST_ARGS ?=
# The servers the docker-* targets start: the company's legacy Postgres (which
# also holds the toolkit's ops store) and ClickHouse, its warehouse.
DOCKER_ENGINES ?= postgres clickhouse

.PHONY: help setup up down seed build company metabase cost incidents reconcile \
        dashboard paused readme-check test lint fmt clean pipeline demo check-env \
        docker-build docker-up docker-demo docker-test docker-lint \
        docker-metadata-check docker-metabase-check docker-readme-check docker-dashboard \
        docker-browser-check docker-shell docker-clean \
        airflow dagster docker-test-orchestrators docs-diagram docker-docs-images

help: ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python dependencies and dbt packages
	$(UV) sync
	@if [ -f dbt/packages.yml ]; then $(RUN) dbt deps --project-dir dbt; \
	else echo "no dbt/packages.yml yet, skipping dbt deps (phase 1)"; fi

up: ## Start Docker services (postgres, clickhouse)
	@if [ ! -f .env ]; then \
		echo "error: .env is missing. Run: cp .env.example .env"; exit 1; fi
	$(COMPOSE) up -d --wait postgres clickhouse

down: ## Stop Docker services
	$(COMPOSE) --profile app --profile dashboard --profile browser --profile metabase \
		--profile chat --profile airflow --profile dagster down

seed: ## Seed the legacy Postgres, empty ClickHouse, backfill its raw layer
	$(RUN) platform-ops seed

build: ## Run dbt build on ClickHouse (add --full-refresh after a seed)
	$(RUN) platform-ops build

company: ## Replay the company's window: daily data, ingestion, dbt builds, BI and ad hoc reads
	$(RUN) platform-ops company run

# The company's BI. Not part of `demo`: Metabase is a heavy JVM service, and its
# dashboards read the marts that `make demo` builds, so run this after it.
metabase: check-env ## Start Metabase and build one dashboard per exposure (after make demo)
	$(RUN) platform-ops metabase prepare
	$(COMPOSE) --profile metabase up -d --wait metabase
	$(RUN) platform-ops metabase provision
	@echo "Metabase: http://localhost:$${METABASE_PORT:-3000} (MB_ADMIN_EMAIL and MB_ADMIN_PASSWORD in .env)"

cost: ## Price the company run's query log, show it back by team, write the cost report
	$(RUN) platform-ops cost report

incidents: ## The incident report and postmortems from the store, graded (after make demo)
	$(RUN) platform-ops incidents run

reconcile: ## Write the cutover sign-off from the parallel run (after make demo)
	$(RUN) platform-ops reconcile run

dashboard: ## Launch the Streamlit dashboards on the ops store (after make demo)
	$(RUN) platform-ops dashboard

paused: ## List any 1.x module still paused in version 2.0 (none since Phase 14)
	$(RUN) platform-ops paused

# Not part of `demo`: the README numbers come from the reference run, and a
# first `make demo` on another machine should not fail on a documentation check.
# The README's results are from scale 1.0 runs: the 2.0.0 release run's, then each
# fix of 3.0.0's, rerun where the fix moved them.
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

# Version 2.0: the company runs on Postgres and ClickHouse. Six weeks of its
# window are the migration's parallel run (reconciled every night, Phase 12),
# and its last three are production on ClickHouse, where faults happen and the
# incident desk ingests every night's dbt invocations (Phase 14). `company run`
# seeds and builds itself, so `demo` needs no separate `seed` and `build`
# steps. Then the ownership check and lineage, the sign-off, the incident
# report from the store, and the cost report (Phase 13), which reads the run's
# query log, the ownership and the cutover, so it comes last.
pipeline: company ## Every module, in order, on the current state
	$(RUN) platform-ops metadata check
	$(RUN) platform-ops metadata lineage
	$(RUN) platform-ops reconcile run
	$(RUN) platform-ops incidents run
	$(RUN) platform-ops cost report
	@echo "pipeline complete."

demo: clean setup up pipeline ## Full end to end run on a clean state

# -- in the container ------------------------------------------------------------
#
# The same work inside the toolkit image (Dockerfile), next to Postgres and
# ClickHouse in Compose. No Python, uv or make on the host beyond this Makefile.
# Code changes need `make docker-build`; generated state lives on volumes.

# The toolkit's container runs as user 1000 and writes to two folders of this
# checkout, bind-mounted: reports/ and docs/images/screens/. Where the checkout
# belongs to another user, as on a GitHub runner (1001), the container may not
# write there, so their owner opens them to it first. Docker Desktop does not
# check, which is why only Linux hosts need it.
check-env:
	@if [ ! -f .env ]; then \
		echo "error: .env is missing. Run: cp .env.example .env"; exit 1; fi
	@mkdir -p reports docs/images/screens && chmod a+w reports docs/images/screens

docker-build: check-env ## Build the toolkit image
	$(COMPOSE) build app

docker-up: check-env ## Start the database engines for the container (DOCKER_ENGINES)
	$(COMPOSE) up -d --wait $(DOCKER_ENGINES)

# SCALE=0.1 runs it smaller, for a check of behaviour rather than numbers (the
# release workflow); results are always quoted at config/settings.yaml's scale.
docker-demo: docker-up ## Full end to end run in the container on a clean state (SCALE=...)
	$(COMPOSE) run --rm $(if $(SCALE),-e PLATFORM_OPS_SCALE_FACTOR=$(SCALE)) app make clean pipeline

docker-test: docker-up ## Run the tests in the container, skips listed with their reason
	$(COMPOSE) run --rm app make test PYTEST_ARGS=-rs

docker-lint: check-env ## Lint, check formatting, and type check in the container
	$(COMPOSE) run --rm app make lint

# CODEOWNERS for data: every model, source and exposure has exactly one owner.
# --parse builds the manifest without any data.
docker-metadata-check: check-env ## Check dataset ownership in the container
	$(COMPOSE) run --rm app platform-ops metadata check --parse

# Run after docker-demo: provisions Metabase, runs every card through its API,
# then opens every dashboard in headless Chromium (screenshots in reports/).
docker-metabase-check: docker-up ## Provision Metabase and check every dashboard (after docker-demo)
	$(COMPOSE) run --rm app platform-ops metabase prepare
	$(COMPOSE) --profile metabase up -d --wait metabase
	$(COMPOSE) run --rm app sh -c "platform-ops metabase provision && platform-ops metabase check"
	$(COMPOSE) --profile metabase run --rm metabase-screens

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

# -- the orchestrators (Phase 14b) -----------------------------------------------
#
# The company's dbt project scheduled by a real orchestrator, each as a company
# of its own (namespace af_ for Airflow, dg_ for Dagster), so make demo and its
# reports are never touched. Their pages go to the chat double. Everything runs
# in containers: the toolkit image seeds the companies and serves the double
# (after a code change, make docker-build), and each orchestrator has its own
# image, rebuilt here when its files change.

AIRFLOW_SCALE ?= 0.1
DAGSTER_SCALE ?= 0.1
ORCHESTRATORS ?= airflow dagster

airflow: docker-up ## Seed the af_ company, start Airflow with Cosmos (AIRFLOW_SCALE=...)
	$(COMPOSE) run --rm -e PLATFORM_OPS_NAMESPACE=af_ -e PLATFORM_OPS_SCALE_FACTOR=$(AIRFLOW_SCALE) \
		app platform-ops seed
	AIRFLOW_SCALE=$(AIRFLOW_SCALE) $(COMPOSE) --profile chat --profile airflow up -d --wait --build \
		chat airflow
	@echo "Airflow: http://localhost:$${AIRFLOW_PORT:-8080}, user admin, AIRFLOW_ADMIN_PASSWORD in .env."
	@echo "DAG company_dbt runs at 02:00 UTC once unpaused; pages: http://localhost:$${CHAT_DOUBLE_PORT:-8099}/messages"

dagster: docker-up ## Seed the dg_ company, start Dagster with dagster-dbt (DAGSTER_SCALE=...)
	$(COMPOSE) run --rm -e PLATFORM_OPS_NAMESPACE=dg_ -e PLATFORM_OPS_SCALE_FACTOR=$(DAGSTER_SCALE) \
		app platform-ops seed
	DAGSTER_SCALE=$(DAGSTER_SCALE) $(COMPOSE) --profile chat --profile dagster up -d --wait --build \
		chat dagster
	@echo "Dagster: http://localhost:$${DAGSTER_PORT:-3001} (no login; published on this machine only)."
	@echo "Job company_dbt runs at 02:00 UTC once its schedule is started; pages: http://localhost:$${CHAT_DOUBLE_PORT:-8099}/messages"

# Seeds each company anew at scale 0.01 (make airflow or make dagster afterwards
# for 0.1). ORCHESTRATORS=airflow runs one; the release runs each on its own runner.
docker-test-orchestrators: docker-up ## A fault, its incident and page, the fix and the resolution, through each orchestrator
	@for orchestrator in $(ORCHESTRATORS); do \
		case $$orchestrator in airflow) ns=af_ ;; dagster) ns=dg_ ;; \
			*) echo "unknown orchestrator: $$orchestrator"; exit 2 ;; esac; \
		scale=$$(echo $$orchestrator | tr a-z A-Z)_SCALE; \
		echo "== $$orchestrator"; \
		env $$scale=0.01 $(COMPOSE) --profile chat --profile $$orchestrator up -d --wait --build \
			chat $$orchestrator; \
		$(COMPOSE) run --rm -e PLATFORM_OPS_NAMESPACE=$$ns -e PLATFORM_OPS_SCALE_FACTOR=0.01 \
			app python scripts/check_orchestrators.py $$orchestrator; \
	done

# -- the docs' images (Phase 15) -------------------------------------------------
#
# The architecture diagram is drawn from the project (scripts/make_architecture.py)
# and checked by the tests. The screenshots come from a real run: after
# docker-demo, docker-metabase-check (Metabase's screenshots) and
# docker-test-orchestrators (a faulted run and its fix in each stack), this
# loads the dashboards in a browser, photographs Airflow and Dagster, and picks
# the set the docs show into docs/images/screens.

docs-diagram: ## Redraw docs/images/architecture.svg from the project
	$(RUN) python scripts/make_architecture.py

docker-docs-images: docker-up ## The docs' screenshots from the last run (see the comment above)
	$(COMPOSE) --profile browser run --rm browser-check; \
	status=$$?; $(COMPOSE) --profile browser stop dashboard; exit $$status
	$(COMPOSE) --profile docs run --rm docs-screens
	$(COMPOSE) run --rm -v "$(CURDIR)/docs/images/screens:/app/docs/images/screens" \
		app python scripts/curate_screens.py
