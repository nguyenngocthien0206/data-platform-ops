"""The CLI surface is the contract the Makefile drives. Pin it down."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from platform_ops import __version__
from platform_ops.cli import PAUSED_UNTIL, app

runner = CliRunner()

# Every command a Makefile target invokes.
MAKEFILE_COMMANDS: list[list[str]] = [
    ["seed"],
    ["build"],
    ["company", "run"],
    ["metabase", "prepare"],
    ["metabase", "provision"],
    ["paused"],
    ["cost", "report"],
    ["incidents", "run"],
    ["reconcile", "run"],
    ["dashboard"],
]


def test_help_exits_zero_and_lists_every_command_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in (
        "metadata",
        "company",
        "metabase",
        "cost",
        "incidents",
        "reconcile",
    ):
        assert group in result.output
    for command in ("seed", "build", "dashboard", "version"):
        assert command in result.output


def test_version_command_prints_the_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_config_command_prints_validated_settings() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert '"scale_factor"' in result.output
    assert '"usd_per_tib"' in result.output


@pytest.mark.parametrize("command", MAKEFILE_COMMANDS, ids=" ".join)
def test_every_makefile_command_is_built_and_documented(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0
    assert "not implemented" not in result.output.lower()


# Version 2.0: each 1.x module that has not moved yet, with the command that runs it.
PAUSED_COMMANDS = {
    "incidents": ["incidents", "run"],
}


def test_every_paused_module_has_its_command() -> None:
    assert set(PAUSED_COMMANDS) == set(PAUSED_UNTIL)


@pytest.mark.parametrize("module", sorted(PAUSED_COMMANDS))
def test_a_paused_command_exits_2_and_names_the_phase_it_returns_in(module: str) -> None:
    """Before reading any settings or data: the 1.x warehouse it needs is gone."""
    result = runner.invoke(app, PAUSED_COMMANDS[module])
    assert result.exit_code == 2
    assert f"returns in Phase {PAUSED_UNTIL[module]}" in result.output
    assert "v1.2.0" in result.output


def test_paused_lists_every_paused_module() -> None:
    result = runner.invoke(app, ["paused"])
    assert result.exit_code == 0
    for module, phase in PAUSED_UNTIL.items():
        assert f"{module:<12} paused, returns in Phase {phase}" in result.output
