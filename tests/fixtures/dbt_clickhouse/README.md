# acme_analytics: a fixture dbt project on ClickHouse

A small dbt project that looks nothing like the bundled demo: a different
project name, staging folders per source system, a local package, seeds, a
snapshot and two exposures. The tests parse it to check ownership on a project
the toolkit has never seen, and run it against the ClickHouse service in Compose
(`docker compose --profile clickhouse up -d`) so its artifacts are real.

`ownership/` holds its registry, the way a company dbt repository keeps its own
`teams.yaml` and `ownership.yaml` next to the project.
