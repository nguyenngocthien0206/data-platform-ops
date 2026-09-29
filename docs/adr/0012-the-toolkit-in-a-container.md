# 0012: The toolkit in a container, with its state on volumes

Status: accepted
Date: 2026-09-29

## Context

Until Phase 7 the toolkit had only ever run natively on one Windows laptop, and the host kept getting in the way. Windows Smart App Control refused freshly released native wheels (pandas, pyarrow, then DuckDB), which forced version caps and a mypy built from source; `core.autocrlf` turned generated files into CRLF; and the SQL Server tests skipped wherever the optional driver was missing. Each workaround was reasonable, and each one was knowledge that lived in one person's head. A toolkit that only runs on the machine of the person who wrote it cannot be handed to a team.

## Decision

The toolkit runs in a container, next to the legacy databases in Docker Compose, and every step people run natively has a container twin (`make docker-demo`, `docker-test`, `docker-lint`, `docker-readme-check`, `docker-dashboard`).

- **The code is baked into the image, the state lives on named volumes.** The DuckDB warehouse, the Iceberg warehouse and dbt's target and logs sit on volumes inside the Docker VM, where disk is fast; a bind mount from a Windows or macOS host would make a DuckDB file of a gigabyte slow. Only `reports/` is bind-mounted, so the reports show up on the host. A code change needs `make docker-build`, which takes seconds because the dependency layer is cached.
- **Service addresses come from the environment.** The connectors already let an environment variable win over `.env`, so Compose points them at the service names; no code changed. `.env` never enters the image; Compose passes it at run time.
- **Toolkit services sit behind profiles**, so a plain `docker compose up` still starts only Postgres, as before.
- **Everything is stored with LF line endings** (`.gitattributes`), because the image is built from the working tree and a CRLF Makefile breaks in Linux.
- **The dashboards are checked in a real browser.** `make docker-browser-check` loads every page in headless Chromium, in the light and the dark theme, fails on exceptions, error alerts, failed requests and missing charts or tables, and saves screenshots for a human look. Playwright lives in its own image, so the toolkit image carries no browser.
- **The Smart App Control workarounds were removed once the container was proven**: the pandas and pyarrow caps in Phase 7 (kept only because the reports stayed byte-identical), mypy's source build in Phase 8.

## Consequences

Two clean container runs wrote byte-identical reports, identical to the native Windows run and to the Phase 6 baseline, and the full test suite ran in the container with Postgres and SQL Server up and nothing skipped (308 tests). On the same laptop the container demo took 7 minutes 16 seconds and the native one 7 minutes 49 seconds. The image is 1.58 GB.

For a team, the gain is that "it works on my machine" stops being a question: onboarding is Docker plus `make docker-demo`, and a failure a colleague reports can be reproduced from the same image. The cost is one more thing to keep current (base image, uv, the Playwright image), and the SQL Server image stays x86_64 only (ADR 0001), so Apple Silicon still runs it under emulation. The browser check found three presentation problems AppTest never could (repeated month labels, a timeline axis without dates, truncated table columns); they are recorded as known issues, not hidden.
