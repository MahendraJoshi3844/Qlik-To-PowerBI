"""Qlik's associative model -> a Power BI star schema.

Qlik associates tables on **every field name they share**, with no direction
and no cardinality: two tables sharing `CustomerID` are linked, two sharing
`Region` *and* `Year` get a *synthetic key*, and a loop of such links is a
*circular reference* that Qlik resolves by loosening one of them. Power BI needs
each link to be one column, a direction, a cardinality, and one active path.

How each of those is decided here, and how sure it is:

* **Link** - one relationship per pair of tables sharing exactly one field.
  Pairs sharing several fields are synthetic keys: not guessed, reported (a
  composite key or a link table is a design decision).
* **Cardinality** - a side is the *one* side only with evidence that the key is
  unique there: proven by a `GROUP BY` on exactly that field, or assumed from
  convention (the key is the table's first field, the table is not built by
  CROSSTABLE or CONCATENATE, and it is not the more fact-like of the two). With
  no evidence either way the relationship is **many-to-many**, which can be
  slower but never fails a refresh the way a wrong one-side does.
* **Fact-likeness** - how many aggregations in the app's measures and charts
  read the table's own fields.
* **Circular references** - the first relationships (proven, then assumed
  one-to-many, then many-to-many) stay active; any that closes a loop is written
  inactive and reported, mirroring Qlik's loosely coupled table.
* **Types** - Qlik is typeless. A field aggregated numerically becomes a number,
  a field read through a date function a date, everything else text; keys are
  text on every side so relationships always join. Each table's query ends with
  the matching `Table.TransformColumnTypes`, so the model and the data agree.
"""

from __future__ import annotations

import re

from qlik2pbi.app.model import QlikApp
from qlik2pbi.expr.ast import Call, FieldRef, walk
from qlik2pbi.expr.parser import ParseError, expand, parse
from qlik2pbi.expr.to_m import m_string
from qlik2pbi.findings import Fidelity, FindingLog
from qlik2pbi.script.interpret import ScriptModel, ScriptTable
from qlik2pbi.semantic.model import (
    SemanticModel,
    SemColumn,
    SemHierarchy,
    SemParameter,
    SemRelationship,
    SemTable,
)

NUMERIC_AGGS = {"sum", "avg", "median", "stdev", "fractile", "kurtosis", "skew", "sterr", "mode"}
ALL_AGGS = NUMERIC_AGGS | {"count", "min", "max", "only", "concat", "maxstring", "minstring", "firstsortedvalue"}
DATE_FUNCS = {"year", "month", "day", "week", "weekday", "quarter", "monthstart", "monthend", "yearstart", "yearend",
              "weekstart", "quarterstart", "inyear", "inyeartodate", "inmonth", "addmonths", "monthname", "date"}
_M_TYPE = {"string": "type text", "double": "type number", "date": "type date", "integer": "Int64.Type"}


def app_expressions(app: QlikApp, variables: dict[str, str]) -> list[str]:
    """Every expression text the front end evaluates, variables expanded."""
    texts = [m.expression for m in app.measures]
    texts += [f for d in app.dimensions for f in d.fields]
    for obj in app.objects.values():
        texts += [m.expression for m in obj.measures if m.expression]
        texts += [d.field for d in obj.dimensions if d.field]
    out = []
    for t in texts:
        try:
            out.append(expand(t, variables)[0])
        except ParseError:
            out.append(t)
    return out


def _usage(texts: list[str]) -> tuple[dict[str, int], set[str], set[str]]:
    """(aggregated field -> count, numerically aggregated fields, date fields)."""
    aggregated: dict[str, int] = {}
    numeric: set[str] = set()
    dates: set[str] = set()
    def direct_refs(node) -> list[str]:
        """Fields an aggregation reads itself - not those of nested aggregations or Aggr() dimensions."""
        if isinstance(node, FieldRef):
            return [node.name]
        if isinstance(node, Call):
            name = node.name.lower()
            if name in ALL_AGGS or name == "aggr":
                return []
            return [r for a in node.args for r in direct_refs(a)]
        return [r for child in getattr(node, "__dict__", {}).values()
                for r in (direct_refs(child) if hasattr(child, "__dict__") else [])]

    for text in texts:
        try:
            node = parse(text)
        except ParseError:
            continue
        for n in walk(node):
            if isinstance(n, Call):
                name = n.name.lower()
                if name in ALL_AGGS and n.args:
                    refs = direct_refs(n.args[0])
                    for r in refs:
                        aggregated[r] = aggregated.get(r, 0) + 1
                    if name in NUMERIC_AGGS:
                        numeric.update(refs)
                if name in DATE_FUNCS and n.args:
                    dates.update(x.name for x in walk(n.args[0]) if isinstance(x, FieldRef))
                if n.set_expr is not None:
                    for mod in n.set_expr.modifiers:
                        if mod.elements and all(e.kind == "number" or (e.kind == "search" and re.match(r"^[<>=]", e.text))
                                                for e in mod.elements):
                            numeric.add(mod.field)
    return aggregated, numeric, dates


_SCRIPT_DATE = re.compile(r"\b(?:year|month|day|week|quarter|monthstart|yearstart|weekday)\s*\(\s*\[?([\w ]+?)\]?\s*\)", re.I)


class _Planner:
    def __init__(self, app: QlikApp, sm: ScriptModel, log: FindingLog):
        self.app, self.sm, self.log = app, sm, log
        self.model = SemanticModel(name=app.name)
        variables = dict(sm.variables)
        variables.update({v.name: v.definition for v in app.variables})
        self.variables = variables
        self.aggregated, self.numeric, self.dates = _usage(app_expressions(app, variables))
        self.hints: dict[str, str] = {}
        for t in sm.tables:
            for text in t.statements:
                self.dates.update(m.group(1).strip() for m in _SCRIPT_DATE.finditer(text))
            for f, h in t.hints.items():
                # One type per field name, so relationship keys always match.
                rank = {"string": 0, "double": 1, "integer": 2, "date": 3}
                if rank.get(h, 0) > rank.get(self.hints.get(f, "string"), 0) or f not in self.hints:
                    self.hints[f] = h

    def plan(self) -> SemanticModel:
        data = [t for t in self.sm.loaded() if not t.section_access]
        security = [t for t in self.sm.loaded() if t.section_access]
        writable = []
        for t in data:
            if not t.fields:
                self.log.add("model", "table", t.name, Fidelity.MANUAL,
                             "No field list could be determined (" + "; ".join(t.manual or ["LOAD * from an unlisted source"]) + ").",
                             action="Rebuild this table in Power Query and add it to the model.",
                             source=t.statements[0][:400] if t.statements else "")
                continue
            writable.append(t)
        owners: dict[str, list[ScriptTable]] = {}
        for t in writable:
            for f in t.fields:
                owners.setdefault(f, []).append(t)
        keys = {f for f, ts in owners.items() if len(ts) > 1}
        scores = {t.name: sum(self.aggregated.get(f, 0) for f in t.fields if f not in keys) for t in writable}
        self.model.fact_score = scores
        self.relationships(writable, keys, scores)
        for t in writable:
            self.model.tables.append(self.table(t, keys))
        for t in security:
            st = self.table(t, set())
            st.kind, st.hidden = "security", True
            st.description = "Qlik section access table (drives the dynamic RLS role)."
            self.model.tables.append(st)
        self.field_map(writable)
        self.parameters()
        self.hierarchies()
        return self.model

    # -- types --
    def data_type(self, f: str, keys: set[str]) -> str:
        """One type per field name across all tables (so relationship keys match)."""
        hint = self.hints.get(f)
        if hint in ("integer", "date", "string") and hint != "string":
            return hint
        if f in self.dates:
            return "date"
        if hint == "double" or f in self.numeric:
            return "double"
        return "string"

    def table(self, t: ScriptTable, keys: set[str]) -> SemTable:
        sem = SemTable(name=t.name, kind="data", source_table=t.name)
        many_keys = {r.from_column for r in self.model.relationships if r.from_table == t.name and not r.many_to_many}
        for f in t.fields:
            sem.columns.append(SemColumn(name=f, data_type=self.data_type(f, keys), source_column=f, field=f,
                                         hidden=f in many_keys))
        types = "{" + ", ".join("{" + m_string(c.name) + ", " + _M_TYPE[c.data_type] + "}" for c in sem.columns) + "}"
        if t.m is not None:
            body = t.m.replace("\n", "\n    ")
            sem.partition_m = f"let\n    Source = {body},\n    Typed = Table.TransformColumnTypes(Source, {types})\nin\n    Typed"
            if t.assumed:
                self.log.add("model", "table", t.name, Fidelity.ASSUMED, " ".join(t.assumed),
                             action="Confirm the table's rows match Qlik after the first refresh.")
            else:
                self.log.add("model", "table", t.name, Fidelity.EXACT,
                             "Load script converted to Power Query (" + "; ".join(t.sources) + ").")
        else:
            fields = ", ".join(f"#{m_string(c.name)} = {_M_TYPE[c.data_type]}" for c in sem.columns)
            sem.partition_m = f"let Source = #table(type table [{fields}], {{}}) in Source"
            sem.has_query = False
            self.log.add("model", "table", t.name, Fidelity.MANUAL,
                         "Written with its columns and an empty query: " + "; ".join(t.manual) + ".",
                         action="Write this table's Power Query by hand (the Qlik statements are in the source column).",
                         source="\n".join(t.statements)[:1500])
        return sem

    # -- relationships --
    def relationships(self, tables: list[ScriptTable], keys: set[str], scores: dict[str, int]) -> None:
        candidates = []
        for i, a in enumerate(tables):
            for b in tables[i + 1:]:
                shared = [f for f in a.fields if f in b.fields]
                if not shared:
                    continue
                if len(shared) > 1:
                    self.log.add("model", "synthetic key", f"{a.name} + {b.name}", Fidelity.MANUAL,
                                 f"The tables share {len(shared)} fields ({', '.join(shared)}): a Qlik synthetic key. "
                                 "No relationship was written.",
                                 action="Add a composite key column (e.g. Region & '|' & OrderYear) to both tables, or a "
                                        "link table, and relate on it; or rename the fields that should not associate.")
                    continue
                k = shared[0]
                candidates.append(self.orient(a, b, k, scores))
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for _, rel, tier, note in sorted(candidates, key=lambda c: c[0]):
            a, b = find(rel.from_table), find(rel.to_table)
            if a == b:
                rel.active = False
                self.log.add("model", "relationship", f"{rel.from_table} -> {rel.to_table}", Fidelity.ASSUMED,
                             f"On '{rel.from_column}': written inactive - it closes a loop of associations (a Qlik "
                             "circular reference, which Qlik loosens automatically).",
                             action="Decide which path the measures should use; activate it with USERELATIONSHIP "
                                    "or remove the loop by renaming a field.")
            else:
                parent[a] = b
                self.log.add("model", "relationship", f"{rel.from_table} -> {rel.to_table}", tier,
                             f"On '{rel.from_column}'. {note}",
                             action="" if tier is Fidelity.EXACT else "Verify the key is unique on the one side after "
                                                                     "the first refresh; if not, set many-to-many.")
            self.model.relationships.append(rel)

    def orient(self, a: ScriptTable, b: ScriptTable, k: str, scores: dict[str, int]):
        def evidence(t: ScriptTable) -> int:
            if k in t.non_unique:
                return 0
            if k in t.unique:
                return 2
            return 1 if t.fields and t.fields[0] == k else 0

        ea, eb = evidence(a), evidence(b)
        sa, sb = scores.get(a.name, 0), scores.get(b.name, 0)
        one = None
        if ea == 2 or eb == 2:
            one = a if ea >= eb else b
        elif ea and eb:
            one = a if (sa, len(a.fields), a.name) <= (sb, len(b.fields), b.name) else b
        elif ea and sa <= sb:
            one = a
        elif eb and sb <= sa:
            one = b
        if one is None:
            many, other = sorted((a, b), key=lambda t: (-scores.get(t.name, 0), t.name))
            rel = SemRelationship(many.name, k, other.name, k, many_to_many=True, reason=f"shared field '{k}'")
            return ((2, many.name, other.name), rel, Fidelity.ASSUMED,
                    "Many-to-many: neither table is known to hold one row per key.")
        many = b if one is a else a
        rel = SemRelationship(many.name, k, one.name, k, reason=f"shared field '{k}'")
        proven = evidence(one) == 2
        tier = Fidelity.EXACT if proven else Fidelity.ASSUMED
        note = (f"'{one.name}' holds one row per {k} (GROUP BY {k})." if proven else
                f"Assumed one row per {k} in '{one.name}' ({k} is its first field and it is the less fact-like table).")
        return ((0 if proven else 1, many.name, one.name), rel, tier, note)

    def field_map(self, tables: list[ScriptTable]) -> None:
        ones = {(r.to_table, r.to_column) for r in self.model.relationships if not r.many_to_many}
        for t in tables:
            for f in t.fields:
                self.model.field_map.setdefault(f.lower(), []).append((t.name, f))
        for f, locs in self.model.field_map.items():
            locs.sort(key=lambda loc: (loc not in ones, -self.model.fact_score.get(loc[0], 0), loc[0]))

    def parameters(self) -> None:
        for folder in self.sm.folders:
            if not any(folder.name in (t.partition_m or "") for t in self.model.tables):
                continue
            value = folder.path
            if not value:
                self.log.add("model", "parameter", folder.name, Fidelity.MANUAL,
                             f"Folder connection '{folder.connection}' is not in the export.",
                             action=f"Set the parameter '{folder.name}' to the folder's path (Transform data > Edit parameters).")
            else:
                self.log.add("model", "parameter", folder.name, Fidelity.INFO,
                             f"lib://{folder.connection}/ became the parameter '{folder.name}' = {value}.",
                             action="Point it at a location the Power BI gateway or service can reach.")
            if value and not value.endswith(("\\", "/")):
                value += "\\"
            self.model.parameters.append(SemParameter(folder.name, value, f"Qlik folder connection lib://{folder.connection}/"))

    def hierarchies(self) -> None:
        for d in self.app.dimensions:
            if d.grouping == "C":
                self.log.add("model", "master dimension", d.title, Fidelity.MANUAL,
                             f"Cyclic group over {', '.join(d.fields)}.",
                             action="Create a Power BI field parameter with these fields.")
                continue
            if d.grouping != "H":
                continue
            locs = [self.model.locations(f) for f in d.fields]
            tables = {loc[0][0] for loc in locs if loc}
            if any(not loc for loc in locs):
                self.log.add("model", "master dimension", d.title, Fidelity.MANUAL,
                             "Drill-down group over fields not in the model.", action="Create the hierarchy by hand.")
            elif len(tables) == 1:
                t = self.model.table(tables.pop())
                t.hierarchies.append(SemHierarchy(d.title, [loc[0][1] for loc in locs]))
                self.log.add("model", "master dimension", d.title, Fidelity.EXACT,
                             f"Drill-down group became hierarchy '{d.title}' on '{t.name}'.")
            else:
                self.log.add("model", "master dimension", d.title, Fidelity.INFO,
                             "Drill-down fields live in different tables; place them in order on a visual's axis to drill.",
                             action="Or bring them into one table in Power Query to get a model hierarchy.")


def plan_model(app: QlikApp, sm: ScriptModel, log: FindingLog) -> SemanticModel:
    return _Planner(app, sm, log).plan()
