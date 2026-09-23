"""Generated rollups must match the spec exactly, so hand edits cannot drift."""

from __future__ import annotations

from pathlib import Path

from platform_ops.simulation import codegen

DBT_ROOT = Path(__file__).resolve().parent.parent / "dbt"


def test_committed_files_match_a_fresh_render() -> None:
    for relative, expected in codegen.render_all().items():
        path = DBT_ROOT / relative
        assert path.exists(), f"{relative} is missing: rerun the codegen"
        actual = path.read_text(encoding="utf-8")
        assert actual == expected, f"{relative} was edited by hand: rerun the codegen"


def test_no_stale_files_in_generated_dirs() -> None:
    wanted = {DBT_ROOT / relative for relative in codegen.render_all()}
    for directory in codegen.generated_dirs(DBT_ROOT):
        for path in directory.iterdir():
            assert path in wanted, f"{path.name} is not produced by the spec: delete it"


def test_every_family_references_a_real_hand_written_model() -> None:
    hand_written = {
        path.stem
        for path in (DBT_ROOT / "models" / "marts").rglob("*.sql")
        if "generated" not in path.parts
    }
    for family in codegen.ROLLUPS:
        assert family.source in hand_written, family.source
        assert family.source.startswith(f"{family.team}_"), "a team only rolls up its own facts"


def test_write_all_removes_stale_files(tmp_path: Path) -> None:
    stale = tmp_path / "models" / "marts" / "sales" / "generated" / "sales_rpt_old.sql"
    stale.parent.mkdir(parents=True)
    stale.write_text("select 1", encoding="utf-8")
    codegen.write_all(tmp_path)
    assert not stale.exists()
