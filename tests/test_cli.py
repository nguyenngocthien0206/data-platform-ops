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


# Version 2.0 paused each 1.x module until its phase moved it; since Phase 14 none is.
def test_nothing_is_paused_any_more() -> None:
    assert PAUSED_UNTIL == {}
    result = runner.invoke(app, ["paused"])
    assert result.exit_code == 0
    assert "Nothing is paused" in result.output
