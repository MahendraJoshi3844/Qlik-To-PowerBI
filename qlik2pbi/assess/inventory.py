"""Pre-migration assessment: inventory, complexity, effort, readiness.

Scores come from what actually makes Qlik content expensive to rebuild in Power
BI: script logic (joins, keeps, ApplyMap, QVD layers, loops, generated
calendars), expression logic (set analysis, dollar expansion, Aggr, TOTAL,
inter-record functions, alternate states) and objects with no native Power BI
visual. The effort constants are printed with the result so they can be tuned.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from qlik2pbi.app.model import QlikApp
from qlik2pbi.expr.ast import Call, Dollar, walk
from qlik2pbi.expr.parser import ParseError, expand, parse
from qlik2pbi.expr.to_dax import INTER_RECORD, SELECTION_FUNCS
from qlik2pbi.report.layout import MANUAL_VISUALS, VISUALS
from qlik2pbi.script.interpret import ScriptModel

SHEET_HOURS = {"simple": 3.0, "medium": 8.0, "complex": 20.0}
EXPRESSION_HOURS = {"simple": 0.25, "medium": 1.0, "complex": 3.0}
SCRIPT_TABLE_HOURS = {"simple": 0.5, "medium": 2.0, "complex": 6.0}
AUTOMATION = {"simple": 0.9, "medium": 0.6, "complex": 0.25}


def _level(score: int) -> str:
    return "simple" if score <= 1 else "medium" if score <= 4 else "complex"


@dataclass
class Scored:
    name: str
    kind: str
    score: int
    level: str
    features: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    app: str
    counts: dict[str, int]
    script: dict[str, int]
    expressions: list[Scored]
    sheets: list[Scored]
    tables: list[Scored]
    effort: dict[str, float]
    readiness_pct: float
    effort_model: dict = field(default_factory=lambda: {
        "sheet_hours": SHEET_HOURS, "expression_hours": EXPRESSION_HOURS,
        "script_table_hours": SCRIPT_TABLE_HOURS, "automation": AUTOMATION})

    def to_dict(self) -> dict:
        return asdict(self)


def score_expression(name: str, kind: str, text: str, variables: dict[str, str], state: str = "") -> Scored:
    feats: list[str] = []
    score = 0
    try:
        expanded, unknown = expand(text, variables)
        if unknown:
            return Scored(name, kind, 5, "complex", [f"unknown variable(s) {', '.join(unknown)}"])
        node = parse(expanded)
    except ParseError as exc:
        return Scored(name, kind, 6, "complex", [f"unparseable ({exc})"])
    if "$(" in text:
        feats.append("variable expansion")
    for n in walk(node):
        if isinstance(n, Dollar):
            score += 2
            feats.append("$(=...) expansion")
        if not isinstance(n, Call):
            continue
        low = n.name.lower()
        if n.set_expr is not None:
            s = n.set_expr
            dyn = any(e.kind in ("dollar", "search", "other") for m in s.modifiers for e in m.elements)
            score += 3 if s.compound or s.identifier not in ("$", "1", "") else (2 if dyn else 1)
            feats.append("set analysis" + (" (dynamic)" if dyn else ""))
        if n.total:
            score += 1
            feats.append("TOTAL")
        if low == "aggr":
            score += 2
            feats.append("Aggr()")
        if low in INTER_RECORD:
            score += 3
            feats.append(f"{n.name}() (inter-record)")
        if low in SELECTION_FUNCS:
            score += 3
            feats.append(f"{n.name}() (selection state)")
        if low == "rank":
            score += 2
            feats.append("Rank()")
    if state:
        score += 2
        feats.append(f"alternate state {state}")
    return Scored(name, kind, score, _level(score), sorted(set(feats)))


def assess(app: QlikApp, sm: ScriptModel) -> Assessment:
    variables = dict(sm.variables)
    variables.update({v.name: v.definition for v in app.variables})
    exprs = [score_expression(m.title, "master measure", m.expression, variables) for m in app.measures]
    sheets: list[Scored] = []
    for sheet in app.sheets:
        score, feats = 0, []
        objs = [app.objects[c.object_id] for c in sheet.cells if c.object_id in app.objects]
        for obj in objs:
            if obj.qtype in ("filterpane", "listbox"):
                continue
            if obj.qtype not in VISUALS:
                score += 2
                feats.append(f"'{obj.qtype}' has no native visual" if obj.qtype not in MANUAL_VISUALS else f"'{obj.qtype}' to rebuild")
            for i, m in enumerate(obj.measures):
                if m.library_id or not m.expression:
                    continue
                s = score_expression(f"{sheet.title} / {obj.title or obj.id} #{i + 1}", "chart expression",
                                     m.expression, variables, obj.state)
                exprs.append(s)
                score += {"simple": 0, "medium": 1, "complex": 2}[s.level]
        score += len(objs) // 4
        sheets.append(Scored(sheet.title, "sheet", score, _level(score), feats))
    tables: list[Scored] = []
    for t in sm.tables:
        if t.dropped and not t.mapping:
            continue
        s = 0
        feats = []
        text = " ".join(t.statements).lower()
        for word, pts in (("join", 2), ("keep", 2), ("applymap", 1), ("crosstable", 1), ("group by", 1),
                          ("(qvd)", 2), ("autogenerate", 2), ("intervalmatch", 3), ("generic", 3), ("resident", 1),
                          ("where", 0)):
            if word in text:
                s += pts
                feats.append(word.strip("()"))
        if t.manual:
            s += 2
        tables.append(Scored(t.name, "mapping table" if t.mapping else "script table", s, _level(s), feats))
    manual = (sum(SHEET_HOURS[x.level] for x in sheets) + sum(EXPRESSION_HOURS[x.level] for x in exprs)
              + sum(SCRIPT_TABLE_HOURS[x.level] for x in tables))
    residual = (sum(SHEET_HOURS[x.level] * (1 - AUTOMATION[x.level]) for x in sheets)
                + sum(EXPRESSION_HOURS[x.level] * (1 - AUTOMATION[x.level]) for x in exprs)
                + sum(SCRIPT_TABLE_HOURS[x.level] * (1 - AUTOMATION[x.level]) for x in tables))
    everything = sheets + exprs + tables
    auto = sum(1 for x in everything if x.level != "complex")
    return Assessment(
        app=app.name,
        counts=app.counts() | {"script_statements": sm.statement_count, "script_tables": len(sm.loaded())},
        script=dict(sorted(sm.features.items())),
        expressions=sorted(exprs, key=lambda x: (-x.score, x.name)),
        sheets=sorted(sheets, key=lambda x: (-x.score, x.name)),
        tables=sorted(tables, key=lambda x: (-x.score, x.name)),
        effort={
            "manual_rebuild_hours": round(manual, 1),
            "with_accelerator_hours": round(residual, 1),
            "saving_pct": round(100 * (1 - residual / manual), 1) if manual else 0.0,
        },
        readiness_pct=round(100 * auto / len(everything), 1) if everything else 100.0,
    )
