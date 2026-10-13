"""The company's legacy reports: hand-written SQL views on Postgres (version 2.0).

Before it had a warehouse, the company built its reports as views on the
operational tables, one per dashboard. They live in ``legacy_reporting/*.sql``
next to this module; files starting with ``_`` are helper views the reports
share and are created first. Phase 12 reconciles each report against the dbt
mart that replaces it on ClickHouse.
"""

from __future__ import annotations

from pathlib import Path

from psycopg import sql

from platform_ops.common.config import Settings
from platform_ops.common.servers import connect_postgres

REPORTS_DIR = Path(__file__).resolve().parent / "legacy_reporting"


def report_files() -> list[Path]:
    """Helper views first, then the reports, each group in name order."""
    files = sorted(REPORTS_DIR.glob("*.sql"))
    return [f for f in files if f.name.startswith("_")] + [
        f for f in files if not f.name.startswith("_")
    ]


def report_names() -> list[str]:
    return [f.stem for f in report_files() if not f.name.startswith("_")]


def create_reports(settings: Settings) -> list[str]:
    """Recreate the reporting schema and every view in it. Returns the report names."""
    company = settings.company
    app = sql.Identifier(company.name(company.legacy_schema)).as_string()
    reporting_name = company.name(company.reporting_schema)
    reporting = sql.Identifier(reporting_name)
    with connect_postgres(settings) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(reporting))
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(reporting))
        for path in report_files():
            text = path.read_text(encoding="utf-8")
            connection.execute(
                text.replace("{app}", app).replace("{reporting}", reporting.as_string())
            )
        connection.commit()
    return report_names()
