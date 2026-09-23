"""The `platform-ops` command line.

The command surface is the contract between the three modules and the Makefile.
It is fixed here, in Phase 0, before any module exists, so cost, incidents and
reconcile can later be built independently without renegotiating entry points.

Commands belonging to unbuilt phases are registered but refuse to run. They exit
non-zero on purpose: a pipeline that is only half built must not be able to
report success.
"""

from __future__ import annotations

import typer

from platform_ops import __version__
from platform_ops.common.config import load_settings
from platform_ops.common.logging import configure_logging, get_logger

app = typer.Typer(
    name="platform-ops",
    help="Operate a multi-team data platform: cost, incidents, reconciliation.",
    no_args_is_help=True,
    add_completion=False,
)

metadata_app = typer.Typer(help="Ownership registry and lineage graph.", no_args_is_help=True)
simulation_app = typer.Typer(
    help="Simulated company: data, workload, faults.", no_args_is_help=True
)
cost_app = typer.Typer(help="Query cost collection, pricing and attribution.", no_args_is_help=True)
incidents_app = typer.Typer(
    help="Data incident detection, grouping and routing.", no_args_is_help=True
)
reconcile_app = typer.Typer(
    help="Legacy to lakehouse migration reconciliation.", no_args_is_help=True
)

app.add_typer(metadata_app, name="metadata")
app.add_typer(simulation_app, name="simulation")
app.add_typer(cost_app, name="cost")
app.add_typer(incidents_app, name="incidents")
app.add_typer(reconcile_app, name="reconcile")


def _not_implemented(what: str, phase: int) -> None:
    """Fail loudly for a command whose phase has not been built yet."""
    typer.secho(
        f"{what} is not implemented until phase {phase}. See docs/SPEC.md.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("config")
def show_config() -> None:
    """Load config/settings.yaml, validate it, and print the resolved values."""
    configure_logging()
    settings = load_settings()
    logger = get_logger("cli")
    logger.info("loaded settings from %s", settings.root / "config" / "settings.yaml")
    typer.echo(settings.model_dump_json(indent=2))


@app.command()
def seed() -> None:
    """Generate raw data for the simulated company."""
    _not_implemented("seed", 1)


@app.command()
def build() -> None:
    """Run dbt build against the simulated company."""
    _not_implemented("build", 1)


@app.command()
def dashboard() -> None:
    """Launch the Streamlit dashboards."""
    _not_implemented("dashboard", 5)


@metadata_app.command("check")
def metadata_check() -> None:
    """Fail if any dbt model, source or exposure has no owner."""
    _not_implemented("metadata check", 1)


@metadata_app.command("lineage")
def metadata_lineage() -> None:
    """Build the lineage graph and persist edges to ops.lineage_edges."""
    _not_implemented("metadata lineage", 1)


@simulation_app.command("run")
def simulation_run() -> None:
    """Run the workload generator over the simulated time window."""
    _not_implemented("simulation run", 2)


@cost_app.command("report")
def cost_report() -> None:
    """Collect, price, attribute, and write reports/cost.md."""
    _not_implemented("cost report", 2)


@incidents_app.command("run")
def incidents_run() -> None:
    """Inject faults, detect and group incidents, write metrics."""
    _not_implemented("incidents run", 3)


@reconcile_app.command("run")
def reconcile_run() -> None:
    """Run the migration scenario and the diff, write the sign-off report."""
    _not_implemented("reconcile run", 4)


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
