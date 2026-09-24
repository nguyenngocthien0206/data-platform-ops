"""The `platform-ops` command line.

The command surface is the contract between the three modules and the Makefile.
It was fixed in Phase 0, before any module existed, so cost, incidents and
reconcile can be built independently without renegotiating entry points.

Commands belonging to unbuilt phases are registered but refuse to run. They exit
non-zero on purpose: a pipeline that is only half built must not be able to
report success.
"""

from __future__ import annotations

from pathlib import Path

import typer

from platform_ops import __version__
from platform_ops.common.clock import SimulatedClock
from platform_ops.common.config import Settings, load_settings
from platform_ops.common.db import open_connection
from platform_ops.common.logging import configure_logging, get_logger, log_event

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

PARSE_OPTION = typer.Option(
    False,
    "--parse",
    help="Run `dbt parse` first, so no prior build or data is needed (the CI path).",
)


def _not_implemented(what: str, phase: int) -> None:
    """Fail loudly for a command whose phase has not been built yet."""
    typer.secho(
        f"{what} is not implemented until phase {phase}. See docs/SPEC.md.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _start(name: str) -> tuple[Settings, SimulatedClock]:
    """Load settings and set up logging stamped with the simulated window start."""
    settings = load_settings()
    clock = SimulatedClock(settings.simulation.start)
    configure_logging(clock=clock)
    get_logger(name)
    return settings, clock


def _manifest(settings: Settings, parse: bool) -> Path:
    from platform_ops.common.dbt_invoke import DbtError, invocation_from_settings, run_dbt

    invocation = invocation_from_settings(settings)
    if parse:
        try:
            run_dbt(invocation, ["parse"])
        except DbtError as error:
            _fail(str(error))
    return invocation.manifest_path


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
    """Generate raw data for the simulated company, up to the window start."""
    from platform_ops.simulation import raw_data

    settings, clock = _start("seed")
    logger = get_logger("seed")
    with open_connection(settings) as connection:
        counts = raw_data.seed(connection, settings)
    for table, rows in counts.items():
        log_event(logger, f"raw.{table}: {rows:,} rows", clock=clock)
    typer.echo(f"seeded {sum(counts.values()):,} rows across {len(counts)} raw tables")


@app.command()
def build() -> None:
    """Run dbt build, then refresh ops.lineage_edges from the new manifest."""
    from platform_ops.common.dbt_invoke import DbtError, invocation_from_settings, run_dbt
    from platform_ops.metadata.lineage import build_graph, persist_edges
    from platform_ops.metadata.manifest import load_manifest

    settings, clock = _start("build")
    logger = get_logger("build")
    invocation = invocation_from_settings(settings)
    try:
        run_dbt(invocation, ["build"])
    except DbtError as error:
        _fail(str(error))

    graph = build_graph(load_manifest(invocation.manifest_path))
    with open_connection(settings) as connection:
        edges = persist_edges(connection, graph)
    log_event(logger, f"ops.lineage_edges: {edges:,} edges", clock=clock)
    typer.echo(f"dbt build succeeded; lineage refreshed with {edges:,} edges")


@app.command()
def dashboard() -> None:
    """Launch the Streamlit dashboards."""
    _not_implemented("dashboard", 5)


@metadata_app.command("check")
def metadata_check(parse: bool = PARSE_OPTION) -> None:
    """Fail if any dbt model, source or exposure has no single, valid owner."""
    from platform_ops.metadata.check import persist_ownership, run_check
    from platform_ops.metadata.manifest import load_manifest
    from platform_ops.metadata.registry import Registry

    settings, _ = _start("metadata")
    try:
        nodes = load_manifest(_manifest(settings, parse))
    except FileNotFoundError as error:
        _fail(str(error))
    registry = Registry.from_config_dir(settings.root / "config")
    report = run_check(registry, nodes)

    for resource_type, (owned, total) in report.coverage.items():
        typer.echo(f"{resource_type + 's':<10} {owned:>4} of {total:<4} owned")
    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    for problem in report.errors:
        typer.secho(f"error: {problem}", fg=typer.colors.RED, err=True)
    if not report.passed:
        _fail(f"metadata check failed with {len(report.errors)} error(s)")

    with open_connection(settings) as connection:
        rows = persist_ownership(connection, report, nodes)
    typer.echo(f"metadata check passed; ops.node_ownership has {rows} rows")


@metadata_app.command("lineage")
def metadata_lineage(parse: bool = PARSE_OPTION) -> None:
    """Build the lineage graph and persist its edges to ops.lineage_edges."""
    from platform_ops.metadata.lineage import build_graph, persist_edges
    from platform_ops.metadata.manifest import load_manifest

    settings, _ = _start("metadata")
    try:
        graph = build_graph(load_manifest(_manifest(settings, parse)))
    except FileNotFoundError as error:
        _fail(str(error))
    with open_connection(settings) as connection:
        edges = persist_edges(connection, graph)
    typer.echo(f"ops.lineage_edges: {edges:,} edges across {graph.number_of_nodes():,} nodes")


@simulation_app.command("run")
def simulation_run() -> None:
    """Simulate the whole window: daily data, dbt runs, dashboards, ad hoc queries.

    Starts from a fresh seed every time, so the collected workload is the same
    on every run.
    """
    from platform_ops.simulation.workload import run_workload

    settings, clock = _start("simulation")
    summary = run_workload(settings)
    queries = ", ".join(f"{kind} {count:,}" for kind, count in sorted(summary.queries.items()))
    typer.echo(
        f"simulated {summary.days} days: {summary.real_builds} real dbt builds, "
        f"{summary.replayed_builds} replayed; queries: {queries}; "
        f"{summary.rows_loaded:,} rows loaded, {summary.rows_changed:,} changed in place"
    )


@cost_app.command("report")
def cost_report() -> None:
    """Price the collected workload, attribute it, and write reports/cost.md."""
    from platform_ops.cost.run import CostError, run_cost

    settings, _ = _start("cost")
    try:
        summary = run_cost(settings)
    except CostError as error:
        _fail(str(error))
    totals = ", ".join(f"{model} ${usd:,.2f}" for model, usd in sorted(summary.total_usd.items()))
    typer.echo(
        f"priced {summary.queries:,} queries ({totals}); {summary.unused} unused tables; "
        f"wrote {summary.report_path} and {summary.accuracy_path.name}"
    )


@incidents_app.command("run")
def incidents_run() -> None:
    """Inject faults, detect and group incidents, write metrics.

    Starts from a fresh seed every time, so two runs produce the same report.
    """
    from platform_ops.common.dbt_invoke import DbtError
    from platform_ops.incidents.run import run_incidents
    from platform_ops.incidents.scenario import ScenarioError

    settings, _ = _start("incidents")
    try:
        summary, _, _ = run_incidents(settings)
    except (DbtError, ScenarioError) as error:
        _fail(str(error))
    typer.echo(
        f"{summary.faults} faults, {summary.raw_alerts} failing checks, "
        f"{summary.incidents} incidents, {summary.pages} pages; dbt ran on {summary.runs} "
        f"nights ({summary.dbt_invocations} invocations); wrote {summary.report_path} and "
        f"{len(summary.postmortems)} postmortems"
    )
    if not summary.one_incident_per_fault:
        _fail("not every injected fault maps to exactly one incident; see the report")


ENGINE_OPTION = typer.Option(
    None,
    "--engine",
    help="Legacy engine to reconcile (repeatable). Defaults to reconcile.engines in settings.",
)
STRICT_OPTION = typer.Option(
    False, "--strict", help="Exit non-zero when the migration as delivered is not signed off."
)


@reconcile_app.command("run")
def reconcile_run(engine: list[str] | None = ENGINE_OPTION, strict: bool = STRICT_OPTION) -> None:
    """Run the migration scenario and the diff, write the sign-off report.

    The verdict lives in the report; the command succeeds whether or not the
    migration is signed off, unless --strict is given.
    """
    from typing import cast

    from platform_ops.common.config import Engine
    from platform_ops.reconcile.run import ReconcileError, run_and_report

    settings, _ = _start("reconcile")
    allowed = ("postgres", "sqlserver", "duckdb")
    unknown = [e for e in engine or [] if e not in allowed]
    if unknown:
        _fail(f"unknown engine {unknown}; choose from {list(allowed)}")
    engines = [cast(Engine, e) for e in engine] if engine else None
    try:
        summary, _ = run_and_report(settings, engines)
    except ReconcileError as error:
        _fail(str(error))
    verdict = "signed off" if summary.signed_off else "NOT signed off"
    typer.echo(
        f"reconciled {', '.join(summary.engines)}: migration as delivered {verdict}; "
        f"detection recall {summary.recall:.2%}, classification accuracy "
        f"{summary.classification_accuracy:.2%}; wrote {summary.report_path}"
    )
    if strict and not summary.signed_off:
        _fail("the migration as delivered does not meet the sign-off thresholds")


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
