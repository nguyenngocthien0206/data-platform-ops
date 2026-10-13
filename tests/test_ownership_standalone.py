"""The ownership check in standalone mode, as a company dbt repository's CI runs it.

The company's project is parsed once (``dbt parse`` needs no server), then the
check runs with ``--manifest`` and ``--registry-dir`` against edited copies of
its registry, through the real CLI. Standalone mode reads no settings and
writes nothing but the optional summary.

Until version 2.0 these tests ran on a small fixture project on ClickHouse;
the company's own project now runs on ClickHouse, so they run on it. What only
the fixture had, a model from an installed package, is covered at the end with
hand-built nodes.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from platform_ops.cli import app
from platform_ops.common.config import CONFIG_PATH_ENV_VAR, load_settings
from platform_ops.common.dbt_invoke import invocation_from_settings, run_dbt
from platform_ops.metadata.check import Finding
from platform_ops.metadata.manifest import Node
from platform_ops.metadata.output import Locations, github_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()

# Every dataset of the company's project, by type.
COVERAGE = {
    "exposure": {"owned": 12, "total": 12},
    "model": {"owned": 118, "total": 118},
    "seed": {"owned": 1, "total": 1},
    "snapshot": {"owned": 1, "total": 1},
    "source": {"owned": 9, "total": 9},
}
TOTAL = 141
SNAPSHOT = "snapshot.company.snap_customers"


@pytest.fixture(scope="module")
def manifest(
    tmp_path_factory: pytest.TempPathFactory, isolated_config: Callable[..., Path]
) -> Path:
    """The company project's manifest from ``dbt parse``."""
    root = tmp_path_factory.mktemp("parse")
    invocation = invocation_from_settings(load_settings(isolated_config(root, 0.01)))
    run_dbt(invocation, ["parse"])
    return invocation.manifest_path


@pytest.fixture
def check(
    manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[int, str, Path]]:
    """Run the standalone check against an edited copy of the company's registry."""
    # Standalone mode must not read settings: point them at nothing.
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "no-settings.yaml"))
    monkeypatch.chdir(tmp_path)

    def run(
        *options: str, edit: Callable[[dict[str, list[dict[str, str]]]], None] | None = None
    ) -> tuple[int, str, Path]:
        registry = tmp_path / "ownership"
        registry.mkdir(exist_ok=True)
        for name in ("teams.yaml", "ownership.yaml"):
            shutil.copyfile(REPO_ROOT / "config" / name, registry / name)
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
                str(manifest),
                "--registry-dir",
                str(registry),
                "--project-dir",
                str(REPO_ROOT / "dbt"),
                "--repo-root",
                str(REPO_ROOT),
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


def test_every_resource_type_of_the_company_is_owned(
    check: Callable[..., tuple[int, str, Path]], tmp_path: Path
) -> None:
    code, output, _ = check("--summary-json", "summary.json")
    assert code == 0, output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["coverage"] == COVERAGE
    assert (summary["owned"], summary["total"], summary["owned_share"]) == (TOTAL, TOTAL, 1.0)
    assert summary["passed"] and summary["errors"] == [] and summary["warnings"] == []
    # Standalone mode writes nothing else: no store, no settings needed.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ownership", "summary.json"]


def test_an_unowned_snapshot_fails_and_points_at_its_file(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    code, output, _ = check("--format", "github", edit=_drop("snapshot.company.*"))
    assert code == 1
    assert f"{SNAPSHOT} has no owner" in output
    assert f'{{match: "{SNAPSHOT}", owner: <owner>' in output
    assert (
        "::error file=dbt/snapshots/snap_customers.sql,line=1,"
        f"title=Ownership check::{SNAPSHOT} has no owner" in output
    )


def test_report_only_prints_the_problems_but_does_not_fail(
    check: Callable[..., tuple[int, str, Path]], tmp_path: Path
) -> None:
    code, output, _ = check(
        "--report-only", "--summary-json", "summary.json", edit=_drop("snapshot.company.*")
    )
    assert code == 0
    assert "has no owner" in output and "report only, not failing" in output
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    owned_share = round((TOTAL - 1) / TOTAL, 4)
    assert (summary["owned"], summary["total"], summary["owned_share"]) == (
        TOTAL - 1,
        TOTAL,
        owned_share,
    )
    assert not summary["passed"] and summary["report_only"]
    assert summary["errors"][0]["node"] == SNAPSHOT


def test_equally_specific_rules_are_ambiguous(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        _drop("snapshot.company.*")(data)
        for match in ("snapshot.company.snap_custo*", "snapshot.company.*_customers"):
            data["datasets"].append(
                {"match": match, "owner": "priya", "team": "platform", "tier": "important"}
            )

    code, output, _ = check(edit=edit)
    assert code == 1
    assert "snap_customers has ambiguous ownership" in output
    assert "make one rule more specific" in output


def test_a_rule_problem_points_at_its_line_in_ownership_yaml(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    code, output, registry = check(
        "--format",
        "github",
        edit=_add(match="model.company.old_*", owner="zed", team="ghosts"),
    )
    assert code == 1
    lines = (registry / "ownership.yaml").read_text(encoding="utf-8").splitlines()
    line = next(i for i, text in enumerate(lines, 1) if "model.company.old_*" in text)
    # The registry sits outside the repository root here, so its path stays absolute.
    pattern = rf"::error file=[^,]*ownership\.yaml,line={line},title=Ownership check::rule "
    assert re.search(pattern + r"'model\.company\.old_\*' names unknown team 'ghosts'", output)
    assert "::warning " in output and "matches no dataset" in output


def test_an_exposure_owner_mismatch_points_at_the_exposure(
    check: Callable[..., tuple[int, str, Path]],
) -> None:
    def edit(data: dict[str, list[dict[str, str]]]) -> None:
        for rule in data["datasets"]:
            if rule["match"] == "exposure.company.product_catalog_performance":
                rule.update(owner="priya", team="platform")

    code, output, _ = check("--format", "github", edit=edit)
    assert code == 1
    exposures = (REPO_ROOT / "dbt" / "models" / "exposures.yml").read_text(encoding="utf-8")
    line = next(
        i
        for i, text in enumerate(exposures.splitlines(), 1)
        if "name: product_catalog_performance" in text
    )
    assert f"::error file=dbt/models/exposures.yml,line={line}," in output
    assert "declares owner 'kenji' in dbt but the registry says 'priya'" in output


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
        edit=_add(match="model.company.x", owner="priya", team="platform", tier="urgent")
    )
    assert code == 2
    assert "metadata check cannot run" in output


def test_init_registry_writes_a_starter_that_the_check_turns_into_a_todo_list(
    manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "no-settings.yaml"))
    out = tmp_path / "ownership"
    result = runner.invoke(
        app, ["metadata", "init-registry", "--manifest", str(manifest), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    rules = yaml.safe_load((out / "ownership.yaml").read_text(encoding="utf-8"))["datasets"]
    assert len(rules) < TOTAL / 2

    check = ["metadata", "check", "--manifest", str(manifest), "--registry-dir", str(out)]
    todo = runner.invoke(app, [*check, "--report-only", "--summary-json", str(tmp_path / "s.json")])
    assert todo.exit_code == 0
    summary = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert (summary["owned"], summary["total"], len(summary["errors"])) == (0, TOTAL, len(rules))

    # Filling in the TODOs is all it takes to pass. Exposures keep the owner they
    # declare in dbt, so everyone named ends up on the one team.
    for rule in rules:
        rule["team"] = "platform"
        if rule["owner"] == "TODO":
            rule["owner"] = "priya"
    (out / "ownership.yaml").write_text(yaml.safe_dump({"datasets": rules}), encoding="utf-8")
    members = sorted({rule["owner"] for rule in rules})
    team = {"id": "platform", "name": "P", "channel": "#p", "members": members}
    (out / "teams.yaml").write_text(yaml.safe_dump({"teams": [team]}), encoding="utf-8")
    done = runner.invoke(app, check)
    assert done.exit_code == 0, done.output

    again = runner.invoke(
        app, ["metadata", "init-registry", "--manifest", str(manifest), "--out", str(out)]
    )
    assert again.exit_code == 2 and "--force" in again.output


# -- a dataset from an installed package ---------------------------------------------------


def test_a_package_model_is_reported_without_a_file(tmp_path: Path) -> None:
    """A model an installed package defines has no file in the repository to point at."""
    nodes = {
        "model.shared.shared_calendar": Node(
            unique_id="model.shared.shared_calendar",
            resource_type="model",
            name="shared_calendar",
            package="shared",
            path="models/shared_calendar.sql",
        ),
        "model.company.sales_fct_orders": Node(
            unique_id="model.company.sales_fct_orders",
            resource_type="model",
            name="sales_fct_orders",
            package="company",
            path="models/marts/sales/sales_fct_orders.sql",
        ),
    }
    locations = Locations(
        nodes=nodes,
        ownership_file=tmp_path / "ownership.yaml",
        project_dir=REPO_ROOT / "dbt",
        root_project="company",
        repo_root=REPO_ROOT,
    )
    findings = [Finding(f"{unique_id} has no owner", node=unique_id) for unique_id in sorted(nodes)]
    own, package = github_lines(findings, "error", locations)
    assert package == "::error title=Ownership check::model.shared.shared_calendar has no owner"
    assert own.startswith(
        "::error file=dbt/models/marts/sales/sales_fct_orders.sql,line=1,title=Ownership check::"
    )
