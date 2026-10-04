"""The ownership check on a dbt project the toolkit has never seen.

``tests/fixtures/dbt_clickhouse`` is a small project on the ClickHouse adapter,
laid out nothing like the bundled one, with its registry in its own
``ownership/`` folder, the way a company dbt repository keeps it. The project is
parsed once (``dbt parse`` needs no server), then the check runs in standalone
mode against variants of its registry, through the real CLI.

The last tests run the project for real on the ClickHouse service (``docker
compose --profile clickhouse up -d``), so its artifacts are real; they skip with
the reason when ClickHouse is not reachable, and CI starts it. Phases 11 and 12
reuse the project and the service.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR
from platform_ops.common.env import env_value

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dbt_clickhouse"
runner = CliRunner()


def dbt(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """dbt in its own process, so the ClickHouse adapter never meets the DuckDB one."""
    executable = shutil.which("dbt", path=str(Path(sys.executable).parent))
    assert executable is not None, "dbt is not installed next to this Python"
    return subprocess.run(
        [executable, *args, "--project-dir", str(project), "--profiles-dir", str(project)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"},
    )


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A parsed copy of the fixture project."""
    pytest.importorskip(
        "dbt.adapters.clickhouse", reason="install with `uv sync --extra clickhouse`"
    )
    copy = tmp_path_factory.mktemp("acme") / "acme_analytics"
    shutil.copytree(FIXTURE, copy)
    for command in (["deps"], ["parse"]):
        result = dbt(copy, *command)
        assert result.returncode == 0, result.stdout + result.stderr
    return copy


@pytest.fixture
def check(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[int, str, Path]]:
    """Run the standalone check against an edited copy of the fixture's registry."""
    # Standalone mode must not read settings: point them at nothing.
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "no-settings.yaml"))
    monkeypatch.chdir(tmp_path)

    def run(
        *options: str, edit: Callable[[dict[str, list[dict[str, str]]]], None] | None = None
    ) -> tuple[int, str, Path]:
        registry = tmp_path / "ownership"
        shutil.copytree(project / "ownership", registry, dirs_exist_ok=True)
        if edit is not None:
            path = registry / "ownership.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            edit(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "metadata",
                "check",
                "--manifest",
                str(project / "target" / "manifest.json"),
                "--registry-dir",
                str(registry),
                "--repo-root",
                str(project),
                *options,
            ],
        )
        return result.exit_code, result.output, registry

    return run


def _drop(match: str) -> Callable[[dict[str, list[dict[str, str]]]], None]:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        data["datasets"] = [rule for rule in data["datasets"] if rule["match"] != match]

    return edit


def _add(**rule: str) -> Callable[[dict[str, list[dict[str, str]]]], None]:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        data["datasets"].append({"tier": "important", **rule})

    return edit


def test_every_resource_type_of_the_fixture_is_owned(
    check: Callable[..., tuple[int, str, Path]], tmp_path: Path
) -> None:
    code, output, _ = check("--summary-json", "summary.json")
    assert code == 0, output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["coverage"] == {
        "exposure": {"owned": 2, "total": 2},
        "model": {"owned": 15, "total": 15},
        "seed": {"owned": 4, "total": 4},
        "snapshot": {"owned": 1, "total": 1},
        "source": {"owned": 3, "total": 3},
    }
    assert (summary["owned"], summary["total"], summary["owned_share"]) == (25, 25, 1.0)
    assert summary["passed"] and summary["errors"] == [] and summary["warnings"] == []
    # Standalone mode writes nothing else: no warehouse, no settings needed.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ownership", "summary.json"]


def test_an_unowned_model_fails_and_points_at_its_file(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    code, output, _ = check(
        "--format", "github", edit=_drop("model.acme_analytics.rpt_repeat_customers")
    )
    assert code == 1
    assert "model.acme_analytics.rpt_repeat_customers has no owner" in output
    assert '{match: "model.acme_analytics.rpt_repeat_customers", owner: <owner>' in output
    assert (
        "::error file=models/reporting/growth/rpt_repeat_customers.sql,line=1,"
        "title=Ownership check::model.acme_analytics.rpt_repeat_customers has no owner" in output
    )


def test_report_only_prints_the_problems_but_does_not_fail(
    check: Callable[..., tuple[int, str, Path]], tmp_path: Path
) -> None:
    code, output, _ = check(
        "--report-only",
        "--summary-json",
        "summary.json",
        edit=_drop("model.acme_analytics.rpt_repeat_customers"),
    )
    assert code == 0
    assert "has no owner" in output and "report only, not failing" in output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert (summary["owned"], summary["total"], summary["owned_share"]) == (24, 25, 0.96)
    assert not summary["passed"] and summary["report_only"]
    assert summary["errors"][0]["node"] == "model.acme_analytics.rpt_repeat_customers"


def test_equally_specific_rules_are_ambiguous(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        _drop("model.acme_analytics.rpt_repeat_customers")(data)
        for match in (
            "model.acme_analytics.rpt_repeat_custo*",
            "model.acme_analytics.*repeat_customers",
        ):
            data["datasets"].append(
                {"match": match, "owner": "quan", "team": "growth", "tier": "important"}
            )

    code, output, _ = check(edit=edit)
    assert code == 1
    assert "rpt_repeat_customers has ambiguous ownership" in output
    assert "make one rule more specific" in output


def test_a_rule_problem_points_at_its_line_in_ownership_yaml(
    check: Callable[..., tuple[int, str, Path]], project: Path
) -> None:
    code, output, registry = check(
        "--format",
        "github",
        edit=_add(match="model.acme_analytics.old_*", owner="zed", team="ghosts"),
    )
    assert code == 1
    lines = (registry / "ownership.yaml").read_text(encoding="utf-8").splitlines()
    line = next(i for i, text in enumerate(lines, 1) if "model.acme_analytics.old_*" in text)
    # The registry sits outside the repository root here, so its path stays absolute.
    pattern = rf"::error file=[^,]*ownership\.yaml,line={line},title=Ownership check::rule "
    assert re.search(
        pattern + r"'model\.acme_analytics\.old_\*' names unknown team 'ghosts'", output
    )
    assert "::warning " in output and "matches no dataset" in output


def test_an_exposure_owner_mismatch_points_at_the_exposure(
    check: Callable[..., tuple[int, str, Path]], project: Path
) -> None:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        for rule in data["datasets"]:
            if rule["match"] == "exposure.acme_analytics.growth_weekly_review":
                rule.update(owner="binh", team="platform")

    code, output, _ = check("--format", "github", edit=edit)
    assert code == 1
    exposures = (project / "models" / "reporting" / "exposures.yml").read_text(encoding="utf-8")
    line = next(
        i
        for i, text in enumerate(exposures.splitlines(), 1)
        if "name: growth_weekly_review" in text
    )
    assert f"::error file=models/reporting/exposures.yml,line={line}," in output
    assert "declares owner 'quan' in dbt but the registry says 'binh'" in output


def test_a_package_model_is_reported_without_a_file(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    code, output, _ = check("--format", "github", edit=_drop("model.acme_shared.*"))
    assert code == 1
    assert "::error title=Ownership check::model.acme_shared.shared_calendar has no owner" in output


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--manifest", "missing.json", "--registry-dir", "."], "No dbt manifest at missing.json"),
        (["--manifest", "missing.json"], "--manifest needs --registry-dir"),
        (["--format", "xml"], "unknown --format 'xml'"),
    ],
)
def test_the_check_exits_2_when_it_cannot_run(
    options: list[str], message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "no-settings.yaml"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["metadata", "check", *options])
    assert result.exit_code == 2
    assert message in result.output


def test_an_invalid_registry_exits_2(check: Callable[..., tuple[int, str, Path]]) -> None:
    code, output, _ = check(
        edit=_add(match="model.acme_analytics.x", owner="an", team="platform", tier="urgent")
    )
    assert code == 2
    assert "metadata check cannot run" in output


# -- a real run on ClickHouse -------------------------------------------------------


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture project seeded and built on the ClickHouse service."""
    pytest.importorskip(
        "dbt.adapters.clickhouse", reason="install with `uv sync --extra clickhouse`"
    )
    env_file = REPO_ROOT / ".env"
    host = env_value("CLICKHOUSE_HOST", env_file, "localhost")
    url = f"http://{host}:{env_value('CLICKHOUSE_PORT', env_file, '8123')}"
    try:
        with urllib.request.urlopen(f"{url}/ping", timeout=3) as response:
            response.read()
    except OSError as error:
        pytest.skip(
            f"ClickHouse not reachable at {url} ({type(error).__name__}); "
            "start it with `docker compose --profile clickhouse up -d`"
        )
    copy = tmp_path_factory.mktemp("acme_built") / "acme_analytics"
    shutil.copytree(FIXTURE, copy)
    # Sources read the seeded tables, which dbt does not know to load first.
    for command in (
        ["deps"],
        ["seed", "--full-refresh"],
        ["build", "--exclude", "resource_type:seed"],
    ):
        result = dbt(copy, *command)
        assert result.returncode == 0, result.stdout + result.stderr
    return copy


def test_every_node_of_the_fixture_builds_on_clickhouse(built: Path) -> None:
    target = built / "target"
    results = json.loads((target / "run_results.json").read_text(encoding="utf-8"))
    statuses = {r["unique_id"]: r["status"] for r in results["results"]}
    # dbt lists exposures in a build too, as a no-op.
    failed = {u: s for u, s in statuses.items() if s not in {"success", "pass", "no-op"}}
    assert not failed, failed
    kinds = {unique_id.split(".")[0] for unique_id in statuses}
    assert kinds == {"model", "test", "snapshot", "exposure"}
    assert sum(1 for unique_id in statuses if unique_id.startswith("model.")) == 15
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["adapter_type"] == "clickhouse"


def test_ownership_holds_on_the_manifest_of_a_real_run(
    built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "no-settings.yaml"))
    manifest = built / "target" / "manifest.json"
    registry = built / "ownership"
    result = runner.invoke(
        app, ["metadata", "check", "--manifest", str(manifest), "--registry-dir", str(registry)]
    )
    assert result.exit_code == 0, result.output
    assert "models       15 of 15   owned" in result.output
