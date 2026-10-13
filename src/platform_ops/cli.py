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
    help="The migration's reconciliation: the parallel run and the cutover sign-off.",
    no_args_is_help=True,
)

company_app = typer.Typer(
    help="The simulated company on legacy Postgres and ClickHouse (version 2.0).",
    no_args_is_help=True,
)

app.add_typer(metadata_app, name="metadata")
app.add_typer(company_app, name="company")

metabase_app = typer.Typer(
    help="Metabase, the company's BI, built from the dbt exposures (version 2.0).",
    no_args_is_help=True,
)
app.add_typer(metabase_app, name="metabase")
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


# Version 2.0 moves the company to Postgres and ClickHouse one module at a time.
# Until its phase rewrites it, a 1.x module still reads the DuckDB warehouse that
# no longer exists, so its command says when it returns instead of failing
# somewhere deep inside. 1.x is available at tag v1.2.0.
PAUSED_UNTIL: dict[str, int] = {
    "cost": 13,
    "simulation": 13,
    "dashboard": 13,
    "incidents": 14,
}


def paused_reason(module: str) -> str:
    """Why a paused module does not run, and when it returns. The tests skip with it too."""
    return (
        f"{module} is paused while version 2.0 moves it to ClickHouse; it returns in "
        f"Phase {PAUSED_UNTIL[module]}. The 1.x version is at tag v1.2.0."
    )


def _check_paused(module: str) -> None:
    if module in PAUSED_UNTIL:
        _fail(paused_reason(module), EXIT_UNUSABLE)


def _start(name: str) -> tuple[Settings, SimulatedClock]:
    """Load settings and set up logging stamped with the simulated window start."""
    settings = load_settings()
    clock = SimulatedClock(settings.simulation.start)
    configure_logging(clock=clock)
    get_logger(name)
    return settings, clock


def _missing_passwords(error: Exception) -> NoReturn:
    _fail(f"the company's ClickHouse users need their passwords: {error}", EXIT_UNUSABLE)


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


@app.command()
def paused() -> None:
    """List the 1.x modules paused in version 2.0, and the phase each returns in."""
    for module, phase in sorted(PAUSED_UNTIL.items(), key=lambda item: (item[1], item[0])):
        typer.echo(f"{module:<12} paused, returns in Phase {phase}")
    typer.echo("The 1.x versions are at tag v1.2.0.")


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
    """Create the ClickHouse users, seed the legacy Postgres, empty ClickHouse, backfill raw."""
    from platform_ops.common.servers import MissingPasswordError
    from platform_ops.simulation.company import start_over
    from platform_ops.simulation.ingest import Ingestion
    from platform_ops.simulation.legacy_db import LegacyCompany

    settings, clock = _start("seed")
    logger = get_logger("seed")
    legacy = LegacyCompany(settings)
    # As the company's run starts: the delivered job when a parallel run is planned.
    ingestion = Ingestion(settings, delivered=settings.company.parallel_run_weeks > 0)
    try:
        seeded, ingested = start_over(settings, legacy, ingestion)
    except MissingPasswordError as error:
        _missing_passwords(error)
    finally:
        legacy.close()
        ingestion.close()
    log_event(logger, f"legacy Postgres: {seeded:,} rows", clock=clock)
    typer.echo(
        f"seeded {seeded:,} rows into the legacy Postgres; ingested {ingested:,} into ClickHouse"
    )


FULL_REFRESH_OPTION = typer.Option(
    False, "--full-refresh", help="Rebuild incremental models from scratch."
)


@app.command()
def build(full_refresh: bool = FULL_REFRESH_OPTION) -> None:
    """Run dbt build on ClickHouse as the dbt user, then refresh ops.lineage_edges."""
    from platform_ops.common.dbt_invoke import DbtError, invocation_from_settings, run_dbt
    from platform_ops.common.servers import MissingPasswordError
    from platform_ops.metadata.lineage import build_graph, store_edges
    from platform_ops.metadata.manifest import load_manifest
    from platform_ops.simulation.access import ensure_users

    settings, clock = _start("build")
    logger = get_logger("build")
    try:
        ensure_users(settings)
    except MissingPasswordError as error:
        _missing_passwords(error)
    invocation = invocation_from_settings(settings)
    try:
        run_dbt(invocation, ["build", "--full-refresh"] if full_refresh else ["build"])
    except DbtError as error:
        _fail(str(error))
    edges = store_edges(settings, build_graph(load_manifest(invocation.manifest_path)))
    log_event(logger, f"ops.lineage_edges: {edges:,} edges", clock=clock)
    typer.echo(f"dbt build succeeded; lineage refreshed with {edges:,} edges")


DAYS_OPTION = typer.Option(None, "--days", help="Simulated days to replay (default: the window).")


@company_app.command("run")
def company_run(days: int | None = DAYS_OPTION) -> None:
    """Seed the history, then replay the window: daily data, ingestion, dbt builds.

    The last weeks are the migration's parallel run: dbt builds every night and
    the migration is reconciled every night, ending in the cutover decision and
    reports/reconciliation.md.
    """
    from platform_ops.common.dbt_invoke import DbtError, invocation_from_settings
    from platform_ops.common.servers import MissingPasswordError
    from platform_ops.metadata.lineage import build_graph, store_edges
    from platform_ops.metadata.manifest import load_manifest
    from platform_ops.reconcile.parallel import ParallelRun
    from platform_ops.reconcile.report import write_report
    from platform_ops.simulation.company import run_company

    settings, _ = _start("company")
    parallel = ParallelRun(settings) if settings.company.parallel_run_weeks else None
    try:
        summary = run_company(settings, days, nightly=parallel)
    except MissingPasswordError as error:
        _missing_passwords(error)
    except DbtError as error:
        _fail(str(error))
    if parallel is not None and parallel.results:
        cutover = parallel.finish()
        decision = (
            f"cutover recommended on {cutover.run_at:%Y-%m-%d}"
            if cutover.recommended and cutover.run_at
            else "cutover not recommended"
        )
        typer.echo(
            f"parallel run: {len(parallel.results)} nights reconciled, {decision}; "
            f"wrote {write_report(settings)}"
        )

    graph = build_graph(load_manifest(invocation_from_settings(settings).manifest_path))
    edges = store_edges(settings, graph)
    typer.echo(
        f"simulated {summary.days} days: {summary.rows_seeded:,} rows of history, "
        f"{summary.rows_loaded:,} new and {summary.rows_changed:,} changed in Postgres, "
        f"{summary.rows_ingested:,} ingested into ClickHouse, {len(summary.builds)} dbt builds; "
        f"lineage {edges:,} edges"
    )


@metabase_app.command("prepare")
def metabase_prepare() -> None:
    """Create the Postgres database Metabase keeps its own state in (before it starts)."""
    from platform_ops.simulation.metabase import prepare_app_database

    settings, _ = _start("metabase")
    prepare_app_database(settings)
    typer.echo(f"Metabase application database ready: {settings.company.metabase_database}")


@metabase_app.command("provision")
def metabase_provision() -> None:
    """Make Metabase show one dashboard per dbt exposure, on ClickHouse. Safe to repeat."""
    from platform_ops.common.servers import MissingPasswordError
    from platform_ops.simulation.metabase import MetabaseError, metabase_url, provision

    settings, _ = _start("metabase")
    try:
        summary = provision(settings)
    except MissingPasswordError as error:
        _missing_passwords(error)
    except (MetabaseError, ValueError) as error:
        _fail(f"metabase provision cannot run: {error}", EXIT_UNUSABLE)
    typer.echo(
        f"Metabase at {metabase_url(settings)}: {summary.dashboards} dashboards, "
        f"{summary.cards} cards in {len(summary.collections)} team collections"
        + (f"; archived {summary.archived} no longer in the project" if summary.archived else "")
    )


@metabase_app.command("check")
def metabase_check() -> None:
    """Run every card of every dashboard through Metabase; fail on any error."""
    from platform_ops.common.servers import MissingPasswordError
    from platform_ops.simulation.metabase import MetabaseError, check

    settings, _ = _start("metabase")
    try:
        result = check(settings)
    except MissingPasswordError as error:
        _missing_passwords(error)
    except (MetabaseError, ValueError) as error:
        _fail(f"metabase check cannot run: {error}", EXIT_UNUSABLE)
    for failure in result.failures:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
    summary = f"{result.dashboards} dashboards, {result.cards} cards, {result.rows:,} rows"
    if result.failures:
        _fail(f"metabase check failed with {len(result.failures)} error(s): {summary}")
    typer.echo(f"metabase check passed: {summary}")


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
    _check_paused("dashboard")
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
    from platform_ops.metadata.check import run_check, store_ownership
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
    rows = store_ownership(settings, report, nodes)
    typer.echo(f"metadata check passed; ops.node_ownership has {rows} rows")


INIT_MANIFEST_OPTION = typer.Option(..., "--manifest", help="The project's manifest.json.")
OUT_OPTION = typer.Option(
    Path("ownership"), "--out", help="Folder to write teams.yaml and ownership.yaml into."
)
FORCE_OPTION = typer.Option(False, "--force", help="Overwrite a registry that already exists.")


@metadata_app.command("init-registry")
def metadata_init_registry(
    manifest: Path = INIT_MANIFEST_OPTION,
    out: Path = OUT_OPTION,
    force: bool = FORCE_OPTION,
) -> None:
    """Write a starter registry from a manifest: one TODO rule per group of datasets.

    Groups are a source system, a folder of models, an installed package, the
    seeds, the snapshots and each exposure, so the first `metadata check` lists
    one TODO per rule instead of one per dataset.
    """
    from platform_ops.metadata.bootstrap import starter_rules, write_registry
    from platform_ops.metadata.manifest import load_manifest, project_name

    configure_logging()
    try:
        nodes = load_manifest(manifest)
        root_project = project_name(manifest)
        written = write_registry(nodes, root_project, out, force)
    except (FileNotFoundError, FileExistsError, ValueError) as error:
        _fail(f"init-registry cannot run: {error}", EXIT_UNUSABLE)
    rules = starter_rules(nodes, root_project)
    for path in written:
        typer.echo(f"wrote {path}")
    typer.echo(
        f"{len(rules)} rules with owner TODO; fill in teams.yaml and the rules, then run "
        "`platform-ops metadata check --report-only` to see what is left"
    )


@metadata_app.command("lineage")
def metadata_lineage(parse: bool = PARSE_OPTION) -> None:
    """Build the lineage graph and persist its edges to ops.lineage_edges."""
    from platform_ops.metadata.lineage import build_graph, store_edges
    from platform_ops.metadata.manifest import load_manifest

    settings, _ = _start("metadata")
    try:
        graph = build_graph(load_manifest(_manifest(settings, parse)))
    except FileNotFoundError as error:
        _fail(str(error))
    edges = store_edges(settings, graph)
    typer.echo(f"ops.lineage_edges: {edges:,} edges across {graph.number_of_nodes():,} nodes")


@simulation_app.command("run")
def simulation_run() -> None:
    """Simulate the whole window: daily data, dbt runs, dashboards, ad hoc queries.

    Starts from a fresh seed every time, so the collected workload is the same
    on every run.
    """
    from platform_ops.simulation.workload import run_workload

    _check_paused("simulation")

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

    _check_paused("cost")

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

    _check_paused("incidents")
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


STRICT_OPTION = typer.Option(
    False, "--strict", help="Exit non-zero when the parallel run does not recommend the cutover."
)


@reconcile_app.command("run")
def reconcile_run(strict: bool = STRICT_OPTION) -> None:
    """Write the cutover sign-off from the parallel run's nights (run by `make demo`).

    The parallel run is part of the company's history: `company run` replays
    it and reconciles every night. This writes reports/reconciliation.md from
    what those nights recorded, and succeeds whether or not the cutover is
    recommended, unless --strict is given.
    """
    from platform_ops.reconcile.report import NoParallelRun, write_report

    settings, _ = _start("reconcile")
    try:
        path = write_report(settings)
    except NoParallelRun as error:
        _fail(str(error), EXIT_UNUSABLE)
    text = path.read_text(encoding="utf-8")
    recommended = "**Cutover recommended" in text
    verdict = "cutover recommended" if recommended else "cutover NOT recommended"
    typer.echo(f"{verdict}; wrote {path}")
    if strict and not recommended:
        _fail("the parallel run does not meet the cutover criteria")


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
