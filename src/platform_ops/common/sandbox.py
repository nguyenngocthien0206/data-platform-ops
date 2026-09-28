"""A throwaway copy of the repo config whose outputs all land in one directory.

Integration tests and the fixture generator drive the real CLI against the real
settings, teams and ownership files, but must never touch the real warehouse,
``dbt/target`` or ``reports/``. This writes a config under ``root`` that points
every output there and the dbt project at the repo's own, so what runs is
exactly what a user runs, somewhere disposable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def write_isolated_config(
    root: Path, scale_factor: float, weeks: int | None = None, repo: Path = REPO_ROOT
) -> Path:
    """Write ``root/config/settings.yaml`` (plus teams, ownership and ``.env``) and return it."""
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load((repo / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw["scale_factor"] = scale_factor
    if weeks is not None:
        raw["simulation"]["weeks"] = weeks
    raw["paths"] = {
        "duckdb": str(root / "warehouse.duckdb"),
        "reports": str(root / "reports"),
        "iceberg_warehouse": str(root / "iceberg"),
        "dbt_project": str(repo / "dbt"),
        "dbt_target": str(root / "target"),
    }
    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    for name in ("teams.yaml", "ownership.yaml"):
        shutil.copyfile(repo / "config" / name, config_dir / name)
    # Local service credentials (Postgres, SQL Server) live in .env at the root.
    if (repo / ".env").is_file():
        shutil.copyfile(repo / ".env", root / ".env")
    return settings_path
