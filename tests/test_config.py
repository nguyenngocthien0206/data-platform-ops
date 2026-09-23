from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from platform_ops.common.config import CONFIG_PATH_ENV_VAR, Settings, load_settings

WriteSettings = Callable[[dict[str, object]], Path]


def test_repo_settings_load_and_validate(repo_settings: Settings) -> None:
    """The settings file that ships with the repo must actually be valid."""
    assert repo_settings.scale_factor > 0
    assert repo_settings.seed == 20260923
    assert repo_settings.simulation.weeks == 6
    assert repo_settings.simulation.daily_run_hour == 2
    assert repo_settings.pricing.scan.usd_per_tib > 0
    assert repo_settings.pricing.compute.minimum_billed_seconds == 60


def test_simulation_window_derives_duration_and_end(settings: Settings) -> None:
    assert settings.simulation.duration == timedelta(weeks=2)
    assert settings.simulation.end == datetime(2026, 1, 19)


def test_relative_paths_resolve_against_repo_root(settings: Settings) -> None:
    resolved = settings.resolve(settings.paths.duckdb)
    assert resolved.is_absolute()
    assert resolved.name == "test.duckdb"


def test_env_var_selects_the_config_file(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(settings_file))
    assert load_settings().seed == 42


def test_missing_file_raises_with_a_useful_message(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No settings file"):
        load_settings(tmp_path / "nope.yaml")


def test_negative_scale_factor_is_rejected(
    settings_dict: dict[str, object], write_settings: WriteSettings
) -> None:
    settings_dict["scale_factor"] = -1.0
    with pytest.raises(ValidationError):
        load_settings(write_settings(settings_dict))


def test_missing_required_key_is_rejected(
    settings_dict: dict[str, object], write_settings: WriteSettings
) -> None:
    del settings_dict["pricing"]
    with pytest.raises(ValidationError):
        load_settings(write_settings(settings_dict))


def test_unknown_key_is_rejected(
    settings_dict: dict[str, object], write_settings: WriteSettings
) -> None:
    """A typo in YAML must fail loudly rather than being silently ignored."""
    settings_dict["scale_facter"] = 2.0
    with pytest.raises(ValidationError):
        load_settings(write_settings(settings_dict))


def test_out_of_range_run_hour_is_rejected(
    settings_dict: dict[str, object], write_settings: WriteSettings
) -> None:
    simulation = settings_dict["simulation"]
    assert isinstance(simulation, dict)
    simulation["daily_run_hour"] = 25
    with pytest.raises(ValidationError):
        load_settings(write_settings(settings_dict))
