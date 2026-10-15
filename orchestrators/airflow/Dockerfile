# Airflow 3 with Cosmos, running the company's dbt project (Phase 14b-2, ADR 0017).
#
# Two Python environments that never meet:
#   - Airflow's own, with Cosmos, installed against Airflow's constraint file. It
#     holds no dbt. Of the toolkit it holds only the package itself, without its
#     dependencies, so the DAG can import the standard-library-only hooks in
#     platform_ops.integrations;
#   - the toolkit's, at /opt/toolkit, installed from uv.lock exactly as in the
#     toolkit's image. Its dbt is the one Cosmos runs, and its CLI is the one the
#     hooks call (PLATFORM_OPS_CLI).
# Either side can be upgraded without asking the other.
#
# Built from the repository root: docker compose --profile airflow build airflow

FROM apache/airflow:3.3.2-python3.11

ARG AIRFLOW_VERSION=3.3.2
ARG COSMOS_VERSION=1.15.1

USER root

# The toolkit, its venv outside the code so no mount can hide it, on the image's
# own Python 3.11 (the version the toolkit is tested on), with the uv the image
# ships. Airflow's environment is the venv at /home/airflow/.local, left alone.
ENV UV_PROJECT_ENVIRONMENT=/opt/toolkit \
    UV_PYTHON=/usr/python/bin/python3.11 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
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
    uv sync --frozen --no-dev --no-editable

# The project's manifest, parsed once here, is what Cosmos renders the DAG from:
# no dbt runs while the DAG is parsed. Parsing needs no connection.
RUN /opt/toolkit/bin/dbt parse --project-dir dbt --profiles-dir dbt \
        --target-path /opt/cosmos/target --log-path /opt/cosmos/logs --quiet \
    && rm -rf dbt/target dbt/logs \
    && chown -R airflow:root /opt/platform-ops /opt/cosmos

USER airflow

# Cosmos in Airflow's environment, pinned by Airflow's constraint file; the
# toolkit package without its dependencies, for the hooks. Its console script is
# removed so the only `platform-ops` that runs is the toolkit's own.
RUN pip install --no-cache-dir \
        "apache-airflow==${AIRFLOW_VERSION}" \
        "astronomer-cosmos==${COSMOS_VERSION}" \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-3.11.txt" \
    && pip install --no-cache-dir --no-deps /opt/platform-ops \
    && rm -f "$(dirname "$(command -v airflow)")/platform-ops"

COPY --chown=airflow:root orchestrators/airflow/dags /opt/airflow/dags
COPY --chown=airflow:root orchestrators/airflow/start.py /opt/airflow/start.py

ENV PLATFORM_OPS_CONFIG=/opt/platform-ops/config/settings.yaml \
    PLATFORM_OPS_CLI=/opt/toolkit/bin/platform-ops \
    COMPANY_DBT_PROJECT=/opt/platform-ops/dbt \
    COMPANY_DBT_MANIFEST=/opt/cosmos/target/manifest.json \
    COMPANY_DBT_EXECUTABLE=/opt/toolkit/bin/dbt \
    AIRFLOW__CORE__LOAD_EXAMPLES=false \
    AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=true \
    AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin \
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_CMD="python /opt/airflow/start.py --connection" \
    CONNECTION_CHECK_MAX_COUNT=0

WORKDIR /opt/airflow

# The image's entrypoint runs a `python ...` command as given. Its check of the
# metadata database is off (CONNECTION_CHECK_MAX_COUNT): start.py creates it first.
CMD ["python", "/opt/airflow/start.py"]
