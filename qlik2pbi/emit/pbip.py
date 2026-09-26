"""Write the .pbip project folder."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from qlik2pbi.emit.pbir import DEFINITION_VERSION, write_report
from qlik2pbi.emit.tmdl import expressions_tmdl, model_tmdl, relationships_tmdl, role_tmdl, table_tmdl
from qlik2pbi.report.layout import ReportPlan
from qlik2pbi.semantic.model import SemanticModel

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_file_name(name: str, fallback: str = "item") -> str:
    """A name from MicroStrategy metadata, made safe to use as one path component."""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    if not cleaned or cleaned in (".", "..") or cleaned.lower() in _RESERVED:
        cleaned = fallback
    return cleaned[:120]


def stable_guid(seed: str) -> str:
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _json(path: Path, obj: object) -> None:
    _text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _platform(kind: str, name: str) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": kind, "displayName": name},
        "config": {"version": "2.0", "logicalId": stable_guid(f"{name}.{kind}")},
    }


def write_pbip(model: SemanticModel, plan: ReportPlan, out_dir: str | Path, project: str) -> Path:
    out = Path(out_dir)
    name = safe_file_name(project, "Project")
    model_dir = out / f"{name}.SemanticModel"
    report_dir = out / f"{name}.Report"
    # Only folders this tool owns are cleared, so a re-run cannot leave stale
    # tables or pages behind; nothing else in `out` is touched.
    for owned in (model_dir / "definition", report_dir / "definition"):
        if owned.is_dir():
            shutil.rmtree(owned)

    d = model_dir / "definition"
    _text(d / "model.tmdl", model_tmdl(model))
    rels = relationships_tmdl(model)
    if rels:
        _text(d / "relationships.tmdl", rels)
    exprs = expressions_tmdl(model.parameters)
    if exprs:
        _text(d / "expressions.tmdl", exprs)
    used: set[str] = set()
    for t in sorted(model.tables, key=lambda x: x.name.lower()):
        fname = safe_file_name(t.name, "table")
        while fname.lower() in used:
            fname += "_"
        used.add(fname.lower())
        _text(d / "tables" / f"{fname}.tmdl", table_tmdl(t))
    for role in sorted(model.roles, key=lambda r: r.name.lower()):
        _text(d / "roles" / f"{safe_file_name(role.name, 'role')}.tmdl", role_tmdl(role))
    _json(model_dir / "definition.pbism", {"version": DEFINITION_VERSION, "settings": {}})
    _json(model_dir / ".platform", _platform("SemanticModel", name))

    write_report(plan, report_dir, name)
    _json(report_dir / "definition.pbir",
          {"version": DEFINITION_VERSION, "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}}})
    _json(report_dir / ".platform", _platform("Report", name))

    pbip = out / f"{name}.pbip"
    _json(pbip, {"version": "1.0", "artifacts": [{"report": {"path": f"{name}.Report"}}],
                 "settings": {"enableAutoRecovery": True}})
    return pbip
