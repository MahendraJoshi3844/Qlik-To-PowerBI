"""Sheets and objects -> Power BI pages and visuals; chart expressions -> measures.

* Each sheet is a page. Qlik lays objects on a grid (`col`, `row`, `colspan`,
  `rowspan`); the grid is scaled onto a 1280x720 page, keeping the arrangement.
* A filter pane becomes one slicer per field, stacked in the pane's area.
* A chart's inline expressions become measures in `Measures`, in the display
  folder `Chart expressions\\<sheet>`. They are translated knowing the chart's
  dimensions and sibling measures (what `{1}`, `Rank()` and `Column(n)` need).
  An expression identical to a master measure reuses it; identical expressions
  on several charts share one measure.
* A calculated dimension (`=If(...)`) becomes a calculated column when every
  field it reads is in one table (or a table that table relates to).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qlik2pbi.app.model import ChartDimension, QlikApp, QlikObject, Sheet
from qlik2pbi.expr.to_dax import ChartContext, Manual, ToDax, convert_format
from qlik2pbi.expr.ast import FieldRef, walk
from qlik2pbi.expr.parser import ParseError, parse
from qlik2pbi.findings import Fidelity, FindingLog, worst
from qlik2pbi.semantic.measures import PLACEHOLDER, normalize
from qlik2pbi.semantic.model import SemanticModel, SemColumn, SemMeasure

PAGE_W, PAGE_H, MARGIN = 1280, 720, 8


@dataclass(frozen=True)
class FieldRef_:
    table: str
    column: str
    is_measure: bool = False


@dataclass
class VisualPlan:
    name: str
    visual_type: str
    wells: dict[str, list[FieldRef_]] = field(default_factory=dict)
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    source_type: str = ""
    fidelity: Fidelity = Fidelity.EXACT

    def fields(self) -> list[FieldRef_]:
        return [f for refs in self.wells.values() for f in refs]


@dataclass
class PagePlan:
    name: str
    origin: str
    visuals: list[VisualPlan] = field(default_factory=list)


@dataclass
class ReportPlan:
    pages: list[PagePlan] = field(default_factory=list)
    object_fidelity: dict[str, Fidelity] = field(default_factory=dict)


#: Qlik object type -> (Power BI visual, well layout, tier, note)
VISUALS: dict[str, tuple[str, str, Fidelity, str]] = {
    "barchart": ("clusteredColumnChart", "category", Fidelity.EXACT, ""),
    "linechart": ("lineChart", "category", Fidelity.EXACT, ""),
    "piechart": ("pieChart", "single", Fidelity.EXACT, ""),
    "combochart": ("lineClusteredColumnComboChart", "combo", Fidelity.EXACT, ""),
    "table": ("tableEx", "table", Fidelity.EXACT, ""),
    "sn-table": ("tableEx", "table", Fidelity.EXACT, ""),
    "pivot-table": ("pivotTable", "matrix", Fidelity.EXACT, ""),
    "sn-pivot-table": ("pivotTable", "matrix", Fidelity.EXACT, ""),
    "kpi": ("card", "card", Fidelity.EXACT, ""),
    "gauge": ("gauge", "gauge", Fidelity.EXACT, ""),
    "scatterplot": ("scatterChart", "scatter", Fidelity.EXACT, ""),
    "treemap": ("treemap", "treemap", Fidelity.EXACT, ""),
    "waterfallchart": ("waterfallChart", "single", Fidelity.ASSUMED, "Waterfall subtotal settings are not carried."),
    "map": ("map", "map", Fidelity.ASSUMED, "Set the location field's data category (Country, City...) in the model; "
            "Qlik map layers are not carried."),
    "qlik-funnel-chart-ext": ("funnel", "single", Fidelity.ASSUMED, "Funnel shape settings are not carried."),
}

MANUAL_VISUALS = {
    "boxplot": "Needs a box-and-whisker custom visual from AppSource.",
    "distributionplot": "Needs a custom visual from AppSource (e.g. a dot plot).",
    "histogram": "Create bins on the field (Power BI groups) and use a column chart.",
    "mekkochart": "Needs a Marimekko custom visual from AppSource.",
    "text-image": "Recreate the text/image with a Power BI text box or image.",
    "action-button": "Recreate with a Power BI button (bookmark or page navigation action).",
    "container": "Tabs of a container become separate visuals toggled by bookmarks and buttons.",
    "video": "Embed with the HTML Content custom visual, or link to it.",
    "bulletchart": "Needs the Bullet Chart custom visual from AppSource.",
}


class _Layout:
    def __init__(self, app: QlikApp, model: SemanticModel, tr: ToDax, names: dict[str, str], log: FindingLog):
        self.app, self.model, self.tr, self.log = app, model, tr, log
        self.names = names
        self.plan = ReportPlan()
        self.by_expr: dict[str, str] = {normalize(m.expression): names[m.title.lower()] for m in app.measures}
        self.used_names = {m.name.lower() for m in model.all_measures()} | {c.name.lower() for t in model.tables for c in t.columns}
        self.page_names: set[str] = set()

    # -- dimensions --
    def dimension(self, d: ChartDimension, where: str) -> list[FieldRef_]:
        fields = [d.field] if d.field else []
        if d.library_id:
            md = self.app.master_dimension(d.library_id)
            if md is None:
                self.log.add("layout", "field", f"{where}: {d.library_id}", Fidelity.MANUAL,
                             "Master dimension not in the export.")
                return []
            fields = md.fields if md.grouping == "H" else md.fields[:1]
            if md.grouping == "C":
                self.log.add("layout", "field", f"{where}: {md.title}", Fidelity.MANUAL,
                             f"Cyclic group '{md.title}': only its first field ({md.fields[0]}) was placed.",
                             action="Replace with a field parameter over " + ", ".join(md.fields) + ".")
        out = []
        for f in fields:
            if f.startswith("="):
                ref = self.calculated_dimension(f, d.label or f, where)
                if ref:
                    out.append(ref)
                continue
            locs = self.model.locations(f)
            if not locs:
                self.log.add("layout", "field", f"{where}: {f}", Fidelity.MANUAL,
                             f"'{f}' is not in the migrated model, so it was left out of the visual.")
                continue
            out.append(FieldRef_(*locs[0]))
        return out

    def calculated_dimension(self, expr: str, label: str, where: str) -> FieldRef_ | None:
        try:
            refs = [n.name for n in walk(parse(expr)) if isinstance(n, FieldRef)]
        except ParseError:
            refs = []
        tables: dict[str, int] = {}
        for r in refs:
            for t, _ in self.model.locations(r):
                tables[t] = tables.get(t, 0) + 1
        base = max(tables, key=lambda t: (tables[t], self.model.fact_score.get(t, 0), t)) if tables else None
        try:
            if base is None:
                raise Manual("the expression reads no field of the model")
            res = self.tr.row(expr, base)
        except Manual as exc:
            self.log.add("layout", "calculated dimension", f"{where}: {label}", Fidelity.MANUAL, str(exc),
                         action="Create it as a calculated column or in Power Query.", source=expr)
            return None
        table = self.model.table(base)
        name = label.lstrip("=")[:60] or "Calculated dimension"
        while name.lower() in self.used_names:
            name += " (calc)"
        self.used_names.add(name.lower())
        table.columns.append(SemColumn(name=name, data_type="string", expression=res.dax))
        self.log.add("layout", "calculated dimension", f"{where}: {label}", worst(res.fidelity, Fidelity.ASSUMED),
                     f"Became the calculated column '{name}' on '{base}' (evaluated per row, as a calculated "
                     "dimension is)." + (" " + " ".join(res.notes) if res.notes else ""), source=expr)
        return FieldRef_(base, name)

    # -- measures --
    def chart_measure(self, obj: QlikObject, idx: int, sheet: Sheet, dims: list[FieldRef_], siblings: list[str]
                      ) -> FieldRef_ | None:
        m = obj.measures[idx]
        where = f"{sheet.title} / {obj.title or obj.id}"
        if m.library_id:
            master = self.app.master_measure(m.library_id)
            if master is None:
                self.log.add("layout", "field", f"{where}: {m.library_id}", Fidelity.MANUAL, "Master measure not in the export.")
                return None
            name = self.names[master.title.lower()]
            return FieldRef_(self.model.MEASURES_TABLE, name, True) if self.model.measure(name) else self._missing(name, where)
        key = normalize(m.expression)
        if key in self.by_expr and self.model.measure(self.by_expr[key]):
            self.log.add("layout", "chart expression", f"{where}: {m.label or m.expression}", Fidelity.INFO,
                         f"Same formula as the master measure '{self.by_expr[key]}', which is used instead.",
                         action="Link the chart to the master item in Qlik too, if it stays in use.")
            return FieldRef_(self.model.MEASURES_TABLE, self.by_expr[key], True)
        context_bound = any(w in key for w in ("{1", "rank(", "column("))
        ctx_key = key + ("|" + "|".join(f"{d.table}.{d.column}" for d in dims) if context_bound else "")
        if ctx_key in self.by_expr:
            return FieldRef_(self.model.MEASURES_TABLE, self.by_expr[ctx_key], True)
        name = (m.label or f"{obj.title or obj.qtype} {idx + 1}").strip() or f"Measure {idx + 1}"
        while name.lower() in self.used_names:
            name = f"{name} ({obj.title or obj.id})" if f"({obj.title or obj.id})" not in name else name + "_"
        self.used_names.add(name.lower())
        ctx = ChartContext(dimensions=[(d.table, d.column) for d in dims if not d.is_measure], measures=siblings)
        home = self.model.measures_table()
        folder = f"Chart expressions\\{sheet.title}"
        try:
            res = self.tr.measure(m.expression, ctx)
            home.measures.append(SemMeasure(name, res.dax, res.format_string or convert_format(m.number_format), folder,
                                            f"From '{obj.title or obj.qtype}' on sheet '{sheet.title}'.",
                                            res.fidelity, m.expression, list(res.notes), f"chart:{sheet.title}",
                                            set(res.depends_on)))
        except Manual as exc:
            home.measures.append(SemMeasure(name, PLACEHOLDER, convert_format(m.number_format), folder,
                                            f"From '{obj.title or obj.qtype}' on sheet '{sheet.title}'.",
                                            Fidelity.MANUAL, m.expression, [str(exc)], f"chart:{sheet.title}"))
        self.by_expr[ctx_key] = name
        return FieldRef_(self.model.MEASURES_TABLE, name, True)

    def _missing(self, name: str, where: str) -> None:
        self.log.add("layout", "field", f"{where}: {name}", Fidelity.MANUAL,
                     f"Measure '{name}' is not in the model, so it was left out of the visual.")
        return None

    # -- visuals --
    def wells(self, layout: str, dims: list[FieldRef_], meas: list[FieldRef_], obj: QlikObject
              ) -> tuple[str | None, dict[str, list[FieldRef_]], list[str]]:
        notes: list[str] = []
        vt = None
        if layout == "category":
            w = {"Category": dims[:1] + dims[2:], "Y": meas, "Series": dims[1:2]}
            if dims[1:2] and len(meas) > 1:
                notes.append("Several measures with two dimensions: the second dimension was put on the axis.")
                w = {"Category": dims, "Y": meas}
        elif layout == "combo":
            w = {"Category": dims[:1], "Y": meas[:1], "Y2": meas[1:]}
        elif layout == "single":
            w = {"Category": dims[:1], "Y": meas[:1]}
            if len(dims) > 1 or len(meas) > 1:
                notes.append("Only the first dimension and measure are used by this visual type.")
        elif layout == "table":
            w = {"Values": dims + meas}
        elif layout == "matrix":
            left = obj.options.get("leftDims")
            left = len(dims) if left is None else int(left)
            w = {"Rows": dims[:left], "Columns": dims[left:], "Values": meas}
        elif layout == "card":
            vt = "card" if len(meas) == 1 else "multiRowCard"
            w = {"Values": meas}
        elif layout == "gauge":
            w = {"Y": meas[:1], "TargetValue": meas[1:2]}
        elif layout == "scatter":
            if len(meas) < 2:
                raise Manual("Scatter plot needs two measures (X and Y).")
            w = {"Category": dims, "X": meas[:1], "Y": meas[1:2], "Size": meas[2:3]}
        elif layout == "treemap":
            w = {"Group": dims, "Values": meas[:1]}
        elif layout == "map":
            w = {"Category": dims[:1], "Size": meas[:1]}
        else:
            raise Manual(f"no well layout '{layout}'")
        return vt, {k: v for k, v in w.items() if v}, notes

    def visual(self, obj: QlikObject, sheet: Sheet) -> list[VisualPlan]:
        where = f"{sheet.title} / {obj.title or obj.id}"
        qtype = obj.qtype
        if qtype in ("filterpane", "listbox"):
            kids = [self.app.objects.get(c) for c in obj.children] if qtype == "filterpane" else [obj]
            out = []
            for kid in [k for k in kids if k is not None]:
                for d in kid.dimensions:
                    refs = self.dimension(d, where)
                    if refs:
                        out.append(VisualPlan(f"Filter {refs[0].column}", "slicer", {"Values": refs[:1]},
                                              source_type=qtype))
            if not out:
                self.log.add("layout", "filter pane", where, Fidelity.MANUAL, "No field of the filter pane is in the model.")
            return out
        spec = VISUALS.get(qtype)
        if spec is None:
            advice = MANUAL_VISUALS.get(qtype, "An extension or visualization bundle object: find the closest "
                                               "Power BI or AppSource visual and rebuild it.")
            self.log.add("layout", "visualization", where, Fidelity.MANUAL, f"'{qtype}': {advice}", action=advice)
            return []
        vt, layout, fid, note = spec
        if qtype == "barchart":
            horizontal = str(obj.options.get("orientation", "")).lower() == "horizontal"
            stacked = str(obj.options.get("grouping", "")).lower() == "stacked"
            vt = {(False, False): "clusteredColumnChart", (False, True): "stackedColumnChart",
                  (True, False): "clusteredBarChart", (True, True): "stackedBarChart"}[(horizontal, stacked)]
        if qtype == "linechart" and str(obj.options.get("lineType", "")).lower() == "area":
            vt = "areaChart"
        if qtype == "piechart" and obj.options.get("donut"):
            vt = "donutChart"
        dims = [r for d in obj.dimensions for r in self.dimension(d, where)]
        siblings = []
        for i, m in enumerate(obj.measures):
            if m.library_id and (mm := self.app.master_measure(m.library_id)):
                siblings.append(self.names[mm.title.lower()])
            else:
                siblings.append(m.label or f"{obj.title or obj.qtype} {i + 1}")
        meas = [r for i in range(len(obj.measures)) if (r := self.chart_measure(obj, i, sheet, dims, siblings))]
        try:
            forced, wells, notes = self.wells(layout, dims, meas, obj)
        except Manual as exc:
            self.log.add("layout", "visualization", where, Fidelity.MANUAL, str(exc), action="Rebuild the visual by hand.")
            return []
        if not wells:
            self.log.add("layout", "visualization", where, Fidelity.MANUAL,
                         "No field of this object is in the model; the visual was not written.")
            return []
        msgs = ([note] if note else []) + notes
        if obj.state:
            fid = worst(fid, Fidelity.MANUAL)
            msgs.append(f"Uses the alternate state '{obj.state}': written in the default state. Recreate the "
                        "comparison with 'Edit interactions' or a disconnected copy of the fields.")
        for ref in (r for refs in wells.values() for r in refs if r.is_measure):
            m = self.model.measure(ref.column)
            if m is not None and m.fidelity in (Fidelity.MANUAL, Fidelity.ASSUMED):
                fid = worst(fid, Fidelity.ASSUMED if m.fidelity is Fidelity.ASSUMED else Fidelity.MANUAL)
        if notes:
            fid = worst(fid, Fidelity.ASSUMED)
        self.log.add("layout", "visualization", where, fid, f"'{qtype}' -> {forced or vt}." + (" " + " ".join(msgs) if msgs else ""),
                     action="Review the visual." if fid is not Fidelity.EXACT else "")
        return [VisualPlan(obj.title or obj.id, forced or vt, wells, source_type=qtype, fidelity=fid)]

    def sheet(self, sheet: Sheet) -> None:
        name, n = sheet.title[:100] or "Sheet", 2
        base = name
        while name.lower() in self.page_names:
            name = f"{base} ({n})"
            n += 1
        self.page_names.add(name.lower())
        page = PagePlan(name=name, origin=f"sheet:{sheet.title}")
        fid = Fidelity.EXACT
        cells = sheet.cells or [None] * 0
        max_c = max((c.col + c.colspan for c in cells), default=24) or 24
        max_r = max((c.row + c.rowspan for c in cells), default=12) or 12
        sx, sy = PAGE_W / max_c, PAGE_H / max_r
        for c in cells:
            obj = self.app.objects.get(c.object_id)
            if obj is None:
                self.log.add("layout", "object", f"{sheet.title} / {c.object_id}", Fidelity.MANUAL,
                             "Object placed on the sheet is not in the export.")
                fid = worst(fid, Fidelity.MANUAL)
                continue
            visuals = self.visual(obj, sheet)
            if not visuals:
                fid = worst(fid, Fidelity.MANUAL)
            x, y = c.col * sx + MARGIN / 2, c.row * sy + MARGIN / 2
            w, h = max(c.colspan * sx - MARGIN, 40), max(c.rowspan * sy - MARGIN, 40)
            for i, v in enumerate(visuals):
                fid = worst(fid, v.fidelity)
                share = h / len(visuals)  # filter-pane slicers stack inside the pane's cell
                v.x, v.y, v.width, v.height = round(x, 1), round(y + i * share, 1), round(w, 1), round(share - (MARGIN if len(visuals) > 1 else 0), 1)
            page.visuals.extend(visuals)
        self.plan.pages.append(page)
        self.plan.object_fidelity[f"sheet:{sheet.title}"] = fid


def plan_report(app: QlikApp, model: SemanticModel, tr: ToDax, names: dict[str, str], log: FindingLog) -> ReportPlan:
    lay = _Layout(app, model, tr, names, log)
    for sheet in app.sheets:
        lay.sheet(sheet)
    for bm in app.bookmarks:
        log.add("layout", "bookmark", bm, Fidelity.MANUAL, "Qlik bookmark (saved selections).",
                action="Recreate as a Power BI bookmark after the report is reviewed.")
    for st in app.stories:
        log.add("layout", "story", st, Fidelity.MANUAL, "Qlik story (data storytelling).",
                action="Rebuild as report pages with bookmarks, or a PowerPoint export.")
    return lay.plan
