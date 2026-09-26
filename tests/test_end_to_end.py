"""The whole pipeline on the synthetic Sales app."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from qlik2pbi.app.loader import UnsupportedInput, load_app
from qlik2pbi.cli import main
from qlik2pbi.emit.pbir import PAGE_VERSION, VISUAL_VERSION
from qlik2pbi.findings import Fidelity
from qlik2pbi.pipeline import Options, run

from conftest import SAMPLE

NAME = "Sales Analytics"


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_project_files(converted):
    out = converted.out_dir
    for rel in [f"{NAME}.pbip", f"{NAME}.SemanticModel/definition/model.tmdl",
                f"{NAME}.SemanticModel/definition/relationships.tmdl",
                f"{NAME}.SemanticModel/definition/expressions.tmdl",
                f"{NAME}.SemanticModel/definition/tables/Orders.tmdl",
                f"{NAME}.SemanticModel/definition/roles/Section Access.tmdl",
                f"{NAME}.Report/definition/version.json", f"{NAME}.Report/definition/pages/pages.json",
                "migration_report.html", "migration_report.json"]:
        assert (out / rel).is_file(), rel


def test_no_converter_defects(converted):
    assert converted.defects == 0


def test_pbir_is_well_formed(converted):
    for p in (converted.out_dir / f"{NAME}.Report" / "definition").rglob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        if p.name == "page.json":
            assert f"/page/{PAGE_VERSION}/" in data["$schema"]
        if p.name == "visual.json":
            assert f"/visualContainer/{VISUAL_VERSION}/" in data["$schema"]
            state = data["visual"]["query"]["queryState"]
            assert state and all(w["projections"] for w in state.values())
            pos = data["position"]
            assert 0 <= pos["x"] and pos["x"] + pos["width"] <= 1280.5 and pos["y"] + pos["height"] <= 720.5


def test_sheets_become_pages_with_slicers(converted):
    pages = {p.name: p for p in converted.plan.pages}
    assert list(pages) == ["Overview", "Details"]
    overview = sorted(v.visual_type for v in pages["Overview"].visuals)
    assert overview == ["card", "clusteredColumnChart", "donutChart", "lineChart", "slicer", "slicer", "slicer", "tableEx"]
    details = sorted(v.visual_type for v in pages["Details"].visuals)
    assert details == ["gauge", "lineClusteredColumnComboChart", "map", "pivotTable", "scatterChart", "treemap"]


def test_chart_expressions_reuse_master_measures(converted):
    names = {m.name for m in converted.model.all_measures()}
    assert "AOV" not in names and "Sales" not in names  # both repeat master measures inline
    msgs = [f.message for f in converted.log.items if f.object_type == "chart expression"]
    assert any("Avg Order Value" in m for m in msgs)


def test_extensions_alternate_states_and_bookmarks_are_reported(converted):
    items = {(f.object_type, f.object_name): f for f in converted.log.items}
    assert items[("visualization", "Details / Flows")].fidelity is Fidelity.MANUAL
    assert "alternate state" in items[("visualization", "Details / Sales map")].message
    assert items[("bookmark", "Europe 2024")].fidelity is Fidelity.MANUAL


def test_section_access_role_covers_every_table_with_the_field(converted):
    role = converted.model.roles[0]
    assert set(role.table_filters) == {"Orders", "Targets", "Budget", "Section Access"}
    assert "USERPRINCIPALNAME()" in role.table_filters["Orders"]


def test_rationalization(converted):
    r = converted.rationalization
    assert ["Revenue", "Total Sales"] in r.duplicate_master_measures
    assert "Legacy Discount" in r.unused_master_measures
    assert "ReturnReason" in r.unused_fields["Returns"]
    assert r.unused_variables == []


def test_no_placeholders(tmp_path):
    r = run(SAMPLE, tmp_path, Options(placeholders=False))
    assert r.model.measure("Running Sales") is None
    assert r.defects == 0
    assert "= BLANK()\n" not in (tmp_path / f"{NAME}.SemanticModel/definition/tables/Measures.tmdl").read_text(encoding="utf-8")


def test_deterministic_and_rerunnable(tmp_path):
    run(SAMPLE, tmp_path / "a")
    run(SAMPLE, tmp_path / "b")
    assert tree(tmp_path / "a") == tree(tmp_path / "b")
    first = tree(tmp_path / "a")
    run(SAMPLE, tmp_path / "a")
    assert tree(tmp_path / "a") == first


def test_zip_json_and_qvs_inputs(tmp_path):
    z = tmp_path / "app.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in sorted(SAMPLE.rglob("*")):
            if f.is_file():
                zf.write(f, "sales_app/" + f.relative_to(SAMPLE).as_posix())
    assert load_app(z).counts() == load_app(SAMPLE).counts()
    qvs = tmp_path / "script.qvs"
    qvs.write_text((SAMPLE / "script.qvs").read_text(encoding="utf-8"), encoding="utf-8")
    assert load_app(qvs).counts()["script_lines"] > 50


@pytest.mark.parametrize("ext", [".qvf", ".qvw"])
def test_binary_apps_are_refused_with_the_way_out(tmp_path, ext):
    f = tmp_path / f"App{ext}"
    f.write_bytes(b"\x00binary")
    with pytest.raises(UnsupportedInput) as exc:
        load_app(f)
    assert "unbuild" in str(exc.value) or "-prj" in str(exc.value)


def test_cli(tmp_path, capsys):
    assert main(["convert", str(SAMPLE), "--out", str(tmp_path), "--name", "Contoso Sales"]) == 0
    assert (tmp_path / "Contoso Sales.pbip").is_file()
    assert main(["inspect", str(SAMPLE)]) == 0
    assert "table Orders" in capsys.readouterr().out
    assert main(["assess", str(SAMPLE), "--out", str(tmp_path / "a")]) == 0


def test_report_lists_every_finding(converted):
    data = json.loads(converted.report_json.read_text(encoding="utf-8"))
    assert len(data["findings"]) == len(converted.log.items)
    assert data["summary"]["tables_with_query"] == 6 and data["summary"]["tables"] == 7
