# Dagster with dagster-dbt, running the company's dbt project (Phase 14b-3, ADR 0017).
#
# Two Python environments that never meet, as in the Airflow image:
#   - Dagster's own, at /opt/dagster, from the pinned and hashed
#     requirements.txt next to this file. dagster-dbt brings a dbt-core of its
#     own, which it reads manifests with; it never runs it. Of the toolkit it
#     holds only the package itself, without its dependencies, so the
#     definitions can import the standard-library-only hook;
#   - the toolkit's, at /opt/toolkit, from uv.lock, exactly as in the toolkit's
#     image. Its dbt is the one dagster-dbt runs (dbt_executable), and its CLI is
#     the one the hook calls (PLATFORM_OPS_CLI).
#
# Built from the repository root: docker compose --profile dagster build dagster

FROM python:3.11.16-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 dagster

# Dagster's environment first: it changes least.
COPY orchestrators/dagster/requirements.txt /tmp/dagster-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/dagster --python /usr/local/bin/python3.11 \
    && uv pip install --python /opt/dagster/bin/python --require-hashes \
        -r /tmp/dagster-requirements.txt

# The toolkit, its venv outside the code so no mount can hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/toolkit \
    UV_PYTHON=/usr/local/bin/python3.11 \
    UV_FROZEN=1 \
    UV_NO_SYNC=1

WORKDIR /opt/platform-ops

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src src
COPY config config
COPY dbt dbt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && uv pip install --python /opt/dagster/bin/python --no-deps /opt/platform-ops

# The project's manifest, parsed once here, is what @dbt_assets is defined
# from: loading the definitions runs no dbt. Parsing needs no connection.
RUN /opt/toolkit/bin/dbt parse --project-dir dbt --profiles-dir dbt \
        --target-path /opt/dagster/manifest --log-path /opt/dagster/manifest/logs --quiet \
    && rm -rf dbt/target dbt/logs

COPY orchestrators/dagster/app /opt/dagster/app
COPY orchestrators/dagster/start.py /opt/dagster/start.py

RUN mkdir -p /opt/dagster/home \
    && cp /opt/dagster/app/dagster.yaml /opt/dagster/home/dagster.yaml \
    && chown -R dagster:dagster /opt/platform-ops /opt/dagster/home /opt/dagster/manifest

USER dagster
WORKDIR /opt/dagster/app

ENV PATH="/opt/dagster/bin:${PATH}" \
    DAGSTER_HOME=/opt/dagster/home \
    PLATFORM_OPS_CONFIG=/opt/platform-ops/config/settings.yaml \
    PLATFORM_OPS_CLI=/opt/toolkit/bin/platform-ops \
    COMPANY_DBT_PROJECT=/opt/platform-ops/dbt \
    COMPANY_DBT_MANIFEST=/opt/dagster/manifest/manifest.json \
    COMPANY_DBT_EXECUTABLE=/opt/toolkit/bin/dbt

CMD ["python", "/opt/dagster/start.py"]
