"""Read a Qlik app into a `QlikApp`.

Accepted inputs (docs/design/INPUT-FORMATS.md):

* a **`qlik app unbuild` folder** (qlik-cli) - `script.qvs`, `connections.yml`,
  `dimensions.json`, `measures.json`, `variables.json`, `objects/*.json`,
  `app-properties.json` - or the same folder zipped;
* a **single `.json`** holding those parts as keys (e.g. an Engine API export);
* a **`.qvs` load script** on its own (model only - no sheets);
* a QlikView **`-prj` folder** (reads `LoadScript.txt`; the XML object files are
  listed as not understood).

`.qvf` and `.qvw` are refused with the way out: both are proprietary binary
containers. Export with `qlik app unbuild` (Qlik Sense) or save a `-prj` folder
(QlikView) and convert that - no Qlik engine is needed at conversion time.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from qlik2pbi.app.model import (
    Cell,
    ChartDimension,
    ChartMeasure,
    Connection,
    MasterDimension,
    MasterMeasure,
    QlikApp,
    QlikObject,
    Sheet,
    Variable,
)


class UnsupportedInput(ValueError):
    pass


# --- a small YAML subset (mappings of scalars), enough for connections.yml ----


def parse_simple_yaml(text: str) -> dict:
    """Nested `key: value` mappings by indentation. No lists, anchors or blocks."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().strip("'\""), value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
                if raw.strip().split(":", 1)[1].strip().startswith("'"):
                    value = value.replace("''", "'")
            parent[key] = value
    return root


# --- shape helpers --------------------------------------------------------------


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    return obj


def _title(obj: dict) -> str:
    t = obj.get("title") or _get(obj, "qMetaDef", "title") or ""
    if isinstance(t, dict):  # title can be an expression object
        t = t.get("qStringExpression", {}).get("qExpr", "") or ""
    return str(t)


def _list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _connections(raw: Any) -> list[Connection]:
    if isinstance(raw, dict) and "connections" in raw:
        raw = raw["connections"]
    items = raw.items() if isinstance(raw, dict) else ((c.get("qName") or c.get("name"), c) for c in _list(raw))
    out = []
    for name, c in items:
        if not name or not isinstance(c, dict):
            continue
        cs = str(c.get("connectionstring") or c.get("qConnectionString") or "")
        kind = str(c.get("type") or c.get("qType") or "").lower()
        path = cs if kind == "folder" else ""
        out.append(Connection(name=str(name), kind=kind, connection_string=cs, path=path))
    return sorted(out, key=lambda c: c.name.lower())


def _variables(raw: Any) -> list[Variable]:
    out = []
    for v in _list(raw):
        name = v.get("qName") or v.get("name")
        if name:
            out.append(Variable(str(name), str(v.get("qDefinition") or v.get("definition") or ""),
                                str(v.get("qComment") or "")))
    return out


def _master_dimensions(raw: Any) -> list[MasterDimension]:
    out = []
    for d in _list(raw):
        dim = d.get("qDim") or {}
        out.append(MasterDimension(
            id=str(_get(d, "qInfo", "qId", default="") or _title(d)),
            title=_title(d) or str(_get(dim, "title", default="")) or ", ".join(dim.get("qFieldDefs", [])),
            fields=[str(f) for f in dim.get("qFieldDefs", [])],
            grouping=str(dim.get("qGrouping") or "N"),
            description=str(_get(d, "qMetaDef", "description", default="") or ""),
        ))
    return out


def _master_measures(raw: Any) -> list[MasterMeasure]:
    out = []
    for m in _list(raw):
        mea = m.get("qMeasure") or {}
        out.append(MasterMeasure(
            id=str(_get(m, "qInfo", "qId", default="") or _title(m)),
            title=_title(m) or str(mea.get("qLabel") or ""),
            expression=str(mea.get("qDef") or ""),
            label=str(mea.get("qLabel") or ""),
            number_format=str(_get(mea, "qNumFormat", "qFmt", default="") or ""),
            description=str(_get(m, "qMetaDef", "description", default="") or ""),
        ))
    return out


def _chart_parts(obj: dict) -> tuple[list[ChartDimension], list[ChartMeasure]]:
    cube = obj.get("qHyperCubeDef") or {}
    dims = []
    for d in _list(cube.get("qDimensions")):
        defs = _get(d, "qDef", "qFieldDefs", default=[]) or []
        labels = _get(d, "qDef", "qFieldLabels", default=[]) or []
        dims.append(ChartDimension(field=str(defs[0]) if defs else "", label=str(labels[0]) if labels else "",
                                   library_id=str(d.get("qLibraryId") or "")))
    measures = []
    for m in _list(cube.get("qMeasures")):
        measures.append(ChartMeasure(
            expression=str(_get(m, "qDef", "qDef", default="") or ""),
            label=str(_get(m, "qDef", "qLabel", default="") or ""),
            library_id=str(m.get("qLibraryId") or ""),
            number_format=str(_get(m, "qDef", "qNumFormat", "qFmt", default="") or ""),
        ))
    lst = obj.get("qListObjectDef")
    if lst:
        defs = _get(lst, "qDef", "qFieldDefs", default=[]) or []
        dims.append(ChartDimension(field=str(defs[0]) if defs else "", library_id=str(lst.get("qLibraryId") or "")))
    return dims, measures


def _object(obj: dict, app: QlikApp) -> None:
    qid = str(_get(obj, "qInfo", "qId", default="") or "")
    qtype = str(_get(obj, "qInfo", "qType", default="") or obj.get("visualization") or "").lower()
    if not qid or not qtype:
        app.unrecognized.append("object without qInfo.qId/qType")
        return
    if qtype == "sheet":
        cells = [
            Cell(object_id=str(c.get("name") or ""), col=float(c.get("col", 0) or 0), row=float(c.get("row", 0) or 0),
                 colspan=float(c.get("colspan", 0) or 0), rowspan=float(c.get("rowspan", 0) or 0))
            for c in _list(obj.get("cells"))
        ]
        app.sheets.append(Sheet(id=qid, title=_title(obj) or qid, cells=cells, rank=float(obj.get("rank", 0) or 0)))
        return
    if qtype in ("story", "slide", "slideitem"):
        if qtype == "story":
            app.stories.append(_title(obj) or qid)
        return
    if qtype == "bookmark":
        app.bookmarks.append(_title(obj) or qid)
        return
    dims, measures = _chart_parts(obj)
    children = [str(_get(c, "qInfo", "qId", default="")) for c in _list(_get(obj, "qChildList", "qItems"))]
    for child in _list(obj.get("qChildren")):  # inline children (listboxes in a filter pane)
        if isinstance(child, dict):
            _object(child, app)
            children.append(str(_get(child, "qInfo", "qId", default="")))
    options = {
        "orientation": obj.get("orientation", ""),
        "grouping": _get(obj, "barGrouping", "grouping", default="") or "",
        "lineType": obj.get("lineType", ""),
        "donut": bool(_get(obj, "donut", "showAsDonut", default=False)),
        "leftDims": _get(obj, "qHyperCubeDef", "qNoOfLeftDims", default=None),
    }
    text = ""
    if qtype == "text-image":
        text = str(obj.get("markdown") or "")
    app.objects[qid] = QlikObject(
        id=qid, qtype=qtype, title=_title(obj), dimensions=dims, measures=measures, options=options,
        children=[c for c in children if c], state=str(obj.get("qStateName") or ""), text=text,
    )


def app_from_parts(parts: dict[str, Any], name: str, source_kind: str) -> QlikApp:
    props = parts.get("app-properties") or parts.get("appProperties") or {}
    app = QlikApp(name=str(props.get("qTitle") or name), source_kind=source_kind)
    app.script = str(parts.get("script") or "")
    app.connections = _connections(parts.get("connections") or {})
    app.variables = _variables(parts.get("variables"))
    app.dimensions = _master_dimensions(parts.get("dimensions"))
    app.measures = _master_measures(parts.get("measures"))
    app.alternate_states = [str(s) for s in _list(props.get("qStateNames"))]
    for b in _list(parts.get("bookmarks")):
        app.bookmarks.append(_title(b) or str(_get(b, "qInfo", "qId", default="")))
    for obj in _list(parts.get("objects")):
        if isinstance(obj, dict):
            _object(obj, app)
    known = {"script", "connections", "variables", "dimensions", "measures", "objects", "bookmarks",
             "app-properties", "appProperties", "config", "project"}
    app.unrecognized.extend(f"part '{k}'" for k in sorted(parts) if k not in known)
    app.sheets.sort(key=lambda s: (s.rank, s.title.lower()))
    return app


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _parts_from_files(files: dict[str, bytes], unknown: list[str]) -> dict[str, Any]:
    """Map an unbuild folder's files (POSIX relative paths) to parts."""
    parts: dict[str, Any] = {"objects": []}
    # A zipped folder puts everything one level down; find the root by script/app files.
    for path in sorted(files):
        p = PurePosixPath(path)
        name = p.name.lower()
        data = files[path]
        if "objects" in [x.lower() for x in p.parts[:-1]] and name.endswith(".json"):
            parts["objects"].append(json.loads(_decode(data)))
        elif name in ("script.qvs", "loadscript.txt") or (name.endswith(".qvs") and "script" not in parts):
            parts["script"] = _decode(data)
        elif name in ("connections.yml", "connections.yaml"):
            parts["connections"] = parse_simple_yaml(_decode(data))
        elif name.endswith(".json") and p.stem in ("connections", "dimensions", "measures", "variables", "bookmarks",
                                                    "app-properties"):
            parts[p.stem] = json.loads(_decode(data))
        elif name == "config.yml":
            continue
        else:
            unknown.append(f"file '{path}'")
    return parts


def load_app(path: str | Path) -> QlikApp:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    suffix = p.suffix.lower()
    if suffix == ".qvf":
        raise UnsupportedInput(
            "A .qvf is a proprietary binary container and is not read directly. Export it with qlik-cli: "
            "`qlik app unbuild --app <app id> --dir out/`, then convert that folder (or zip it)."
        )
    if suffix == ".qvw":
        raise UnsupportedInput(
            "A .qvw is a proprietary binary QlikView document. In QlikView Desktop create the '-prj' folder "
            "(Settings > Document Properties) and save, then convert that folder."
        )
    unknown: list[str] = []
    if p.is_dir():
        files = {f.relative_to(p).as_posix(): f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file()}
        parts = _parts_from_files(files, unknown)
        kind = "prj" if any(k.lower().endswith("loadscript.txt") for k in files) else "unbuild"
    elif suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            files = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
        parts = _parts_from_files(files, unknown)
        kind = "unbuild"
    elif suffix == ".json":
        payload = json.loads(_decode(p.read_bytes()))
        if not isinstance(payload, dict):
            raise UnsupportedInput(f"{p.name} must be an object whose keys are app parts (script, measures, ...)")
        parts, kind = payload, "json"
    elif suffix in (".qvs", ".txt"):
        parts, kind = {"script": _decode(p.read_bytes())}, "script"
    else:
        raise UnsupportedInput(f"Unsupported input '{p.name}': expected an unbuild folder, .zip, .json or .qvs")
    app = app_from_parts(parts, name=p.stem, source_kind=kind)
    app.unrecognized.extend(unknown)
    return app
