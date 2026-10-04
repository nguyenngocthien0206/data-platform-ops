# The toolkit in a container, so it runs the same way on any machine with Docker
# and no longer depends on the host's Python, uv or security policies.
#
# The code is baked into the image. Generated state (the DuckDB warehouse, the
# Iceberg warehouse, dbt's target and logs) lives on named volumes declared in
# docker-compose.yml, and reports/ is bind-mounted so they show up on the host.
# Build and run it through the docker-* make targets.

FROM python:3.11.16-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

# make runs the same recipes as on the host; bash is already in the base image.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*

# The virtual environment sits outside /app so no mount can ever hide it, and
# `uv run` inside the container uses exactly what the lock file installed.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_NO_SYNC=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN useradd --create-home --uid 1000 app

WORKDIR /app

# Dependencies first, in their own layer, so a code change only recopies the
# code. The SQL Server driver, dbt-clickhouse for the ClickHouse fixture project,
# and the dev tools (tests, lint) are included.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra sqlserver --extra clickhouse --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra sqlserver --extra clickhouse \
    && mkdir -p data warehouse reports dbt/target dbt/logs \
    && chown -R app:app /app

USER app

CMD ["platform-ops", "--help"]
