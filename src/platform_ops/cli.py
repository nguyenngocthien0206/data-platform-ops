"""The `platform-ops` command line.

The command surface is the contract between the three modules and the Makefile:
every Makefile target is one command here, so a target's behaviour can be run,
tested and documented without make. Each command imports its module lazily, so
`platform-ops --help` stays fast and one module's dependencies never slow down
another's command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

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


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


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
            _fail(str(error), EXIT_UNUSABLE)
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


PORT_OPTION = typer.Option(8501, "--port", help="Port to serve the dashboards on.")
HEADLESS_OPTION = typer.Option(False, "--headless", help="Do not open a browser.")
DASHBOARD_START_SECONDS = 60


def _wait_until_healthy(url: str, process: Any, timeout: float) -> bool:
    """Poll Streamlit's health endpoint until it answers or the server exits."""
    import time
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as response:
                if response.read().decode().strip() == "ok":
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


@app.command()
def dashboard(port: int = PORT_OPTION, headless: bool = HEADLESS_OPTION) -> None:
    """Launch the Streamlit dashboards (read-only over the ops schema)."""
    import os
    import subprocess
    import sys
    import webbrowser

    from platform_ops.common.config import CONFIG_PATH_ENV_VAR

    settings, _ = _start("dashboard")
    # The dashboards ship with the repo, next to src/; the config may live elsewhere.
    repo = Path(__file__).resolve().parents[2]
    app_path = repo / "dashboards" / "app.py"
    if not app_path.is_file():
        _fail(f"no dashboards at {app_path}")
    env = dict(os.environ)
    env.setdefault(CONFIG_PATH_ENV_VAR, str(settings.root / "config" / "settings.yaml"))
    # Streamlit always runs headless: otherwise its first run on a machine stops
    # at an interactive "Email:" prompt. The browser is opened from here instead.
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    url = f"http://localhost:{port}"
    typer.echo(f"serving the dashboards on {url} (Ctrl+C to stop)")
    # Run from the repo root so Streamlit also reads .streamlit/config.toml.
    process = subprocess.Popen(command, cwd=repo, env=env)
    try:
        if not headless and _wait_until_healthy(url, process, DASHBOARD_START_SECONDS):
            webbrowser.open(url)
        code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        code = process.wait()
    raise typer.Exit(code)


MANIFEST_OPTION = typer.Option(
    None,
    "--manifest",
    envvar="PLATFORM_OPS_MANIFEST",
    help="Check this manifest.json from any dbt project. Needs no settings and no warehouse.",
)
REGISTRY_DIR_OPTION = typer.Option(
    None,
    "--registry-dir",
    envvar="PLATFORM_OPS_REGISTRY_DIR",
    help="Folder with teams.yaml and ownership.yaml (default: the toolkit's config/).",
)
PROJECT_DIR_OPTION = typer.Option(
    None,
    "--project-dir",
    help="The dbt project, to point findings at its files (default: the manifest's project).",
)
REPO_ROOT_OPTION = typer.Option(
    None, "--repo-root", help="Report file paths relative to this folder (default: current)."
)
REPORT_ONLY_OPTION = typer.Option(
    False, "--report-only", help="Report every problem but exit 0, to measure before enforcing."
)
SUMMARY_JSON_OPTION = typer.Option(
    None, "--summary-json", help="Write coverage and every finding to this JSON file."
)
FORMAT_OPTION = typer.Option(
    "text", "--format", help="text, or github to annotate a pull request in GitHub Actions."
)
# Exit codes for a required CI check: 1 means ownership problems the author can
# fix in the registry; 2 means the check could not run at all.
EXIT_PROBLEMS = 1
EXIT_UNUSABLE = 2


@metadata_app.command("check")
def metadata_check(
    parse: bool = PARSE_OPTION,
    manifest: Path | None = MANIFEST_OPTION,
    registry_dir: Path | None = REGISTRY_DIR_OPTION,
    project_dir: Path | None = PROJECT_DIR_OPTION,
    repo_root: Path | None = REPO_ROOT_OPTION,
    report_only: bool = REPORT_ONLY_OPTION,
    summary_json: Path | None = SUMMARY_JSON_OPTION,
    output_format: str = FORMAT_OPTION,
) -> None:
    """Fail if any dbt source, seed, snapshot, model or exposure lacks one valid owner.

    Without --manifest it checks the bundled project and records the result in
    ops.node_ownership. With --manifest it checks any dbt project's manifest
    against the registry in --registry-dir, and writes nothing but the optional
    summary: the mode for a company dbt repository's CI.
    """
    import json

    from pydantic import ValidationError
    from yaml import YAMLError

    from platform_ops.metadata import output
    from platform_ops.metadata.check import persist_ownership, run_check
    from platform_ops.metadata.manifest import load_manifest, project_name
    from platform_ops.metadata.registry import Registry

    if output_format not in ("text", "github"):
        _fail(f"unknown --format '{output_format}'; choose text or github", EXIT_UNUSABLE)
    settings: Settings | None = None
    if manifest is not None:
        if registry_dir is None:
            _fail("--manifest needs --registry-dir (teams.yaml and ownership.yaml)", EXIT_UNUSABLE)
        configure_logging()
        manifest_path = manifest
    else:
        settings, _ = _start("metadata")
        manifest_path = _manifest(settings, parse)
        registry_dir = registry_dir or settings.root / "config"

    try:
        nodes = load_manifest(manifest_path)
        root_project = project_name(manifest_path)
        registry = Registry.from_config_dir(registry_dir)
    except (FileNotFoundError, ValueError, ValidationError, YAMLError) as error:
        _fail(f"metadata check cannot run: {error}", EXIT_UNUSABLE)
    report = run_check(registry, nodes)

    locations = output.Locations(
        nodes=nodes,
        ownership_file=registry_dir / "ownership.yaml",
        project_dir=project_dir or manifest_path.resolve().parent.parent,
        root_project=root_project,
        repo_root=repo_root or Path.cwd(),
    )
    for line in output.coverage_lines(report):
        typer.echo(line)
    for line in output.text_lines(report.warning_findings, "warning", locations):
        typer.secho(line, fg=typer.colors.YELLOW)
    for line in output.text_lines(report.error_findings, "error", locations):
        typer.secho(line, fg=typer.colors.RED, err=True)
    if output_format == "github":
        for line in output.github_lines(report.warning_findings, "warning", locations):
            typer.echo(line)
        for line in output.github_lines(report.error_findings, "error", locations):
            typer.echo(line)
    if summary_json is not None:
        data = output.summary(report, manifest=manifest_path, report_only=report_only)
        summary_json.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not report.passed:
        message = f"metadata check failed with {len(report.errors)} error(s)"
        if report_only:
            typer.echo(f"{message} (report only, not failing)")
            return
        _fail(message, EXIT_PROBLEMS)
    if settings is None:
        typer.echo("metadata check passed")
        return
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

    settings, _ = _start("simulation")
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
