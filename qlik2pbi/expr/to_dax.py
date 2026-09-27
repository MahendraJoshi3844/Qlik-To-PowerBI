"""Qlik chart expressions and master measures -> DAX.

The trust contract: a translation is EXACT, ASSUMED (the assumption is returned
in `notes`), or it raises `Manual` and nothing is emitted.

## Set analysis -> CALCULATE

A set modifier *replaces* the selection on its field, which is exactly what a
CALCULATE boolean filter does to its column - so most set analysis is exact:

| Qlik | DAX | tier |
|---|---|---|
| `{<F={1,2}>}` | `'T'[F] IN {1, 2}` | exact |
| `{<F=>}` | `REMOVEFILTERS('T'[F])` | exact |
| `{<F={"*"}>}` | `NOT ISBLANK('T'[F])` | exact |
| `{<F={">=10<20"}>}` | `'T'[F] >= 10 && 'T'[F] < 20` | exact |
| `{<F*={..}>}` / `{<F-={..}>}` | `KEEPFILTERS(...)` / `KEEPFILTERS(NOT ...)` | exact |
| `{<F={"A*"}>}` | `LEFT('T'[F], 1) = "A"` | assumed (case) |
| `{<F={"$(=Max(F))"}>}` | `VAR v = CALCULATE(MAX(..), ALLSELECTED()) ... 'T'[F] = v` | assumed |
| `{1}` | `REMOVEFILTERS()` (+ the chart's own dimensions) | assumed |
| `TOTAL [<d>]` | `ALLSELECTED()` (+ `VALUES(d)`) | assumed |
| `+=`, `P()`, `E()`, set operators, bookmarks, alternate states | - | manual |

Chart context matters for `{1}`, `Rank()` and `Column(n)`: a chart measure is
translated knowing the chart's dimensions and sibling measures; a master
measure is translated without it and says so when that matters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from qlik2pbi.expr.ast import Binary, Call, Dollar, FieldRef, Node, Num, SetElement, SetExpr, Str, Unary, walk
from qlik2pbi.expr.parser import ParseError, expand, parse
from qlik2pbi.findings import Fidelity, worst
from qlik2pbi.semantic.model import SemanticModel


class Manual(Exception):
    pass


@dataclass
class ChartContext:
    dimensions: list[tuple[str, str]] = field(default_factory=list)  # (table, column)
    measures: list[str] = field(default_factory=list)  # measure names, in chart order


@dataclass
class DaxResult:
    dax: str
    fidelity: Fidelity
    notes: list[str] = field(default_factory=list)
    format_string: str = ""
    depends_on: set[str] = field(default_factory=set)


def q_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def col(table: str, column: str) -> str:
    return f"{q_table(table)}[{column.replace(']', ']]')}]"


def mref(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def dstr(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


AGG = {
    "sum": ("SUM", "SUMX"),
    "avg": ("AVERAGE", "AVERAGEX"),
    "min": ("MIN", "MINX"),
    "max": ("MAX", "MAXX"),
    "median": ("MEDIAN", "MEDIANX"),
    "stdev": ("STDEV.S", "STDEVX.S"),
    "count": ("COUNTA", "COUNTAX"),
}

INTER_RECORD = {"above", "below", "before", "after", "top", "bottom", "first", "last", "rowno", "noofrows",
                "columnno", "noofcolumns", "dimensionality", "secondarydimensionality", "rangesum_above"}
SELECTION_FUNCS = {"getselectedcount", "getfieldselections", "getcurrentselections", "getpossiblecount",
                   "getexcludedcount", "getalternativecount", "getnotselectedcount", "statename", "p", "e"}

_FMT_SAFE = re.compile(r'^[0#,.%$€£¥ \-+();"A-Za-z\\E]*$')


def convert_format(fmt: str) -> str:
    f = (fmt or "").strip()
    if not f or not _FMT_SAFE.match(f) or not any(ch in f for ch in "0#"):
        return ""
    return re.sub(r"(?<!\\)\$", r"\\$", f)


class _Ctx:
    def __init__(self, chart: ChartContext | None):
        self.chart = chart
        self.fidelity = Fidelity.EXACT
        self.notes: list[str] = []
        self.vars: list[tuple[str, str]] = []
        self.depends: set[str] = set()

    def assume(self, note: str) -> None:
        self.fidelity = worst(self.fidelity, Fidelity.ASSUMED)
        if note not in self.notes:
            self.notes.append(note)


class ToDax:
    def __init__(self, model: SemanticModel, variables: dict[str, str], measure_names: dict[str, str]):
        self.model = model
        self.variables = variables
        #: master measure title (lower) -> Power BI measure name
        self.measure_names = measure_names

    # ------------------------------------------------------------------ API

    def measure(self, text: str, chart: ChartContext | None = None) -> DaxResult:
        ctx = _Ctx(chart)
        expanded, unknown = self._expand(text)
        if unknown:
            raise Manual(f"Uses variable(s) not defined in the app: {', '.join(sorted(set(unknown)))}.")
        try:
            node = parse(expanded)
        except ParseError as exc:
            raise Manual(f"Expression not understood ({exc}).") from exc
        fmt = ""
        if isinstance(node, Call) and node.name.lower() == "num" and len(node.args) == 2 and isinstance(node.args[1], Str):
            fmt = convert_format(node.args[1].value)
            if not fmt:
                ctx.assume(f"Num() format '{node.args[1].value}' not carried; set the measure's format by hand.")
            node = node.args[0]
        for n in walk(node):
            if isinstance(n, Call) and n.name.lower() in INTER_RECORD:
                raise Manual(f"{n.name}() reads other rows of the chart. Rebuild it as a visual calculation "
                             "(PREVIOUS/NEXT/RUNNINGSUM/ROWNUMBER) on the visual that shows it.")
            if isinstance(n, Call) and n.name.lower() in SELECTION_FUNCS:
                raise Manual(f"{n.name}() inspects the user's selections. Rebuild with ISFILTERED/VALUES/SELECTEDVALUE.")
        body = self._m(node, ctx)
        if ctx.vars:
            body = " ".join(f"VAR {n} = {v}" for n, v in ctx.vars) + f" RETURN {body}"
        return DaxResult(body, ctx.fidelity, ctx.notes, fmt, ctx.depends)

    def row(self, text: str, table: str) -> DaxResult:
        """A calculated dimension as a calculated column of `table`."""
        ctx = _Ctx(None)
        expanded, unknown = self._expand(text)
        if unknown:
            raise Manual(f"Uses variable(s) not defined in the app: {', '.join(sorted(set(unknown)))}.")
        try:
            node = parse(expanded)
        except ParseError as exc:
            raise Manual(f"Expression not understood ({exc}).") from exc
        dax = self._row(node, table, ctx)
        return DaxResult(dax, ctx.fidelity, ctx.notes)

    def _expand(self, text: str) -> tuple[str, list[str]]:
        try:
            return expand(text, self.variables)
        except ParseError as exc:
            raise Manual(str(exc)) from exc

    # ------------------------------------------------------------ fields

    def field_col(self, name: str) -> tuple[str, str]:
        """The column a field *filters* through: the one side of its relationships."""
        locs = self.model.locations(name)
        if not locs:
            raise Manual(f"Field '{name}' is not in the migrated model.")
        return locs[0]

    def agg_col(self, name: str, ctx: _Ctx) -> tuple[str, str]:
        """The column a field is *aggregated* from: the most fact-like table holding it."""
        locs = self.model.locations(name)
        if not locs:
            raise Manual(f"Field '{name}' is not in the migrated model.")
        best = max(locs, key=lambda loc: (self.model.fact_score.get(loc[0], 0), loc[0]))
        if len(locs) > 1:
            ctx.assume(f"'{name}' exists in {len(locs)} tables; aggregated from '{best[0]}' "
                       "(Qlik aggregates a key field over every table that holds it).")
        return best

    # ------------------------------------------------------------ measure context

    def _m(self, n: Node, ctx: _Ctx) -> str:
        if isinstance(n, Num):
            return n.value
        if isinstance(n, Str):
            return dstr(n.value)
        if isinstance(n, Dollar):
            return self._dollar(n.inner, ctx)
        if isinstance(n, FieldRef):
            name = self.measure_names.get(n.name.lower())
            if name and not self.model.locations(n.name):
                ctx.depends.add(name)
                return mref(name)
            c = self.field_col(n.name)
            ctx.assume(f"'{n.name}' outside an aggregation: Qlik's implicit Only(), i.e. SELECTEDVALUE (blank when "
                       "several values are in context).")
            return f"SELECTEDVALUE({col(*c)})"
        if isinstance(n, Unary):
            inner = self._m(n.operand, ctx)
            return f"NOT ({inner})" if n.op == "not" else f"-({inner})"
        if isinstance(n, Binary):
            return self._binary(n, lambda x: self._m(x, ctx))
        if isinstance(n, Call):
            return self._call(n, ctx)
        raise Manual(f"Unsupported construct {type(n).__name__}.")

    def _binary(self, n: Binary, sub) -> str:
        a, b = sub(n.left), sub(n.right)
        if n.op == "/":
            return f"DIVIDE({a}, {b})"  # Qlik returns null on division by zero; DIVIDE returns blank
        if n.op == "like":
            raise Manual("'like' comparison: rebuild with SEARCH/CONTAINSSTRING.")
        op = {"and": "&&", "or": "||", "xor": "<>"}.get(n.op, n.op)
        return f"({a} {op} {b})"

    def _dollar(self, inner: str, ctx: _Ctx) -> str:
        """`$(=expr)`: evaluated once, in the selection state, before the chart is computed."""
        try:
            node = parse(inner)
        except ParseError as exc:
            raise Manual(f"$(={inner}) not understood ({exc}).") from exc
        value = self._m(node, ctx)
        name = f"__v{len(ctx.vars) + 1}"
        ctx.vars.append((name, f"CALCULATE({value}, ALLSELECTED())"))
        ctx.assume(f"$(={inner}) evaluated once over the current selections (ALLSELECTED), as Qlik evaluates it "
                   "before the chart.")
        return name

    def _call(self, c: Call, ctx: _Ctx) -> str:
        name = c.name.lower()
        if name == "aggr":
            raise Manual("Aggr() outside an aggregation (e.g. as a calculated dimension): rebuild as a calculated table "
                         "or a SUMMARIZE-based measure.")
        if name == "rank":
            return self._rank(c, ctx)
        if name == "column" and len(c.args) == 1 and isinstance(c.args[0], Num):
            idx = int(float(c.args[0].value)) - 1
            if ctx.chart is None or not (0 <= idx < len(ctx.chart.measures)):
                raise Manual("Column(n) outside a chart whose measures are known.")
            ctx.depends.add(ctx.chart.measures[idx])
            return mref(ctx.chart.measures[idx])
        if name in AGG or name in ("only", "maxstring", "minstring", "concat", "fractile", "mode", "firstsortedvalue",
                                   "nullcount", "missingcount", "numericcount", "textcount"):
            return self._aggregate(c, ctx)
        if name in ("rangesum", "rangemax", "rangemin", "rangeavg", "rangecount"):
            if name == "rangesum":
                return "(" + " + ".join(f"COALESCE({self._m(a, ctx)}, 0)" for a in c.args) + ")"
            if name in ("rangemax", "rangemin") and len(c.args) == 2:
                return f"{name[5:].upper()}({self._m(c.args[0], ctx)}, {self._m(c.args[1], ctx)})"
            raise Manual(f"{c.name}() with these arguments.")
        return self._scalar(c, lambda x: self._m(x, ctx), ctx)

    def _rank(self, c: Call, ctx: _Ctx) -> str:
        if not c.args or ctx.chart is None or len(ctx.chart.dimensions) != 1:
            raise Manual("Rank() ranks over the chart's dimension; outside a single-dimension chart rebuild it with "
                         "RANKX or the visual calculation RANK().")
        inner = self._m(c.args[0], ctx)
        dim = ctx.chart.dimensions[0]
        ctx.assume("Rank() over the chart's single dimension, highest first, ties sharing a rank (Qlik's default "
                   "mode may differ).")
        return f"RANKX(ALLSELECTED({col(*dim)}), CALCULATE({inner}),, DESC, SKIP)"

    def _aggregate(self, c: Call, ctx: _Ctx) -> str:
        name = c.name.lower()
        if len(c.args) < 1:
            raise Manual(f"{c.name}() without arguments.")
        arg = c.args[0]
        # Aggr(): the outer aggregation iterates the Aggr dimensions.
        if isinstance(arg, Call) and arg.name.lower() == "aggr":
            core = self._aggr(c, arg, ctx)
        else:
            core = self._plain_aggregate(c, arg, ctx)
        mods = self._set(c.set_expr, ctx) if c.set_expr is not None else []
        if c.total:
            mods = ["ALLSELECTED()"] + mods
            for f in c.total_fields:
                mods.append(f"VALUES({col(*self.field_col(f))})")
            ctx.assume("TOTAL ignores the chart's dimensions but keeps the selections: ALLSELECTED()"
                       + (" (kept: " + ", ".join(c.total_fields) + ")" if c.total_fields else "") + ".")
        return f"CALCULATE({core}, {', '.join(mods)})" if mods else core

    def _plain_aggregate(self, c: Call, arg: Node, ctx: _Ctx) -> str:
        name = c.name.lower()
        refs = [x.name for x in walk(arg) if isinstance(x, FieldRef)]
        if any(isinstance(x, Call) and x.name.lower() in AGG for x in walk(arg)):
            raise Manual(f"Nested aggregation inside {c.name}() needs Aggr(); rebuild with an iterator (SUMX over VALUES).")
        if isinstance(arg, FieldRef):
            t, k = self.agg_col(arg.name, ctx)
            ref = col(t, k)
            if name == "count":
                return f"DISTINCTCOUNTNOBLANK({ref})" if c.distinct else f"COUNTA({ref})"
            if name == "only":
                return f"SELECTEDVALUE({ref})"
            if name in ("maxstring", "minstring"):
                return f"{name[:3].upper()}({ref})"
            if name == "concat":
                delim = self._m(c.args[1], ctx) if len(c.args) > 1 else '""'
                ctx.assume("Concat() order: values are joined in the column's sort order.")
                src = f"DISTINCT({ref})" if c.distinct else f"VALUES({ref})"
                return f"CONCATENATEX({src}, {ref}, {delim})"
            if name == "fractile":
                if len(c.args) < 2:
                    raise Manual("Fractile() without a fraction.")
                return f"PERCENTILE.INC({ref}, {self._m(c.args[1], ctx)})"
            if name not in AGG:
                raise Manual(f"{c.name}() has no verified DAX equivalent.")
            if c.distinct:
                if name == "sum":
                    return f"SUMX(DISTINCT({ref}), {ref})"
                raise Manual(f"{c.name}(DISTINCT ...) has no direct DAX equivalent.")
            return f"{AGG[name][0]}({ref})"
        if name not in AGG or name == "count" and c.distinct:
            raise Manual(f"{c.name}() over an expression has no verified DAX iterator.")
        if not refs:
            raise Manual(f"{c.name}() over an expression without fields.")
        base = self._base_table(refs, ctx)
        row = self._row(arg, base, ctx)
        if c.distinct:
            raise Manual(f"{c.name}(DISTINCT expression) has no direct DAX equivalent.")
        return f"{AGG[name][1]}({q_table(base)}, {row})"

    def _base_table(self, refs: list[str], ctx: _Ctx) -> str:
        """The table to iterate: holds the most of the fields, most fact-like first."""
        tables: dict[str, int] = {}
        for r in refs:
            for t, _ in self.model.locations(r):
                tables[t] = tables.get(t, 0) + 1
        if not tables:
            raise Manual(f"None of {', '.join(refs)} is in the migrated model.")
        return max(tables, key=lambda t: (tables[t], self.model.fact_score.get(t, 0), t))

    def _aggr(self, outer: Call, aggr: Call, ctx: _Ctx) -> str:
        name = outer.name.lower()
        if name not in AGG or name == "count" and outer.distinct:
            raise Manual(f"{outer.name}(Aggr(...)) has no verified DAX iterator.")
        if len(aggr.args) < 2:
            raise Manual("Aggr() without dimensions.")
        dims = []
        for d in aggr.args[1:]:
            if not isinstance(d, FieldRef):
                raise Manual("Aggr() with a calculated or structured dimension.")
            dims.append(self.field_col(d.name))
        inner = self._m(aggr.args[0], ctx)
        if len(dims) == 1:
            table = f"VALUES({col(*dims[0])})"
        else:
            refs = [x.name for x in walk(aggr.args[0]) if isinstance(x, FieldRef)]
            base = self._base_table(refs, ctx)
            table = f"SUMMARIZE({q_table(base)}, {', '.join(col(*d) for d in dims)})"
        ctx.assume("Aggr() became an iterator over its dimensions (" + ", ".join(k for _, k in dims)
                   + "); NODISTINCT and Aggr's own set analysis are not carried.")
        return f"{AGG[name][1]}({table}, CALCULATE({inner}))"

    # ------------------------------------------------------------ set analysis

    def _set(self, s: SetExpr, ctx: _Ctx) -> list[str]:
        if s.compound:
            raise Manual(f"Set operators between sets ({s.raw}): rebuild as separate CALCULATE filters combined by hand.")
        mods: list[str] = []
        ident = s.identifier.strip()
        if ident == "1":
            mods.append("REMOVEFILTERS()")
            if ctx.chart is not None and ctx.chart.dimensions:
                mods += [f"VALUES({col(*d)})" for d in ctx.chart.dimensions]
                ctx.assume("{1} ignores the selections; the chart's own dimensions are kept, as Qlik keeps them.")
            else:
                ctx.assume("{1} ignores all selections. Correct in cards/KPIs; in a chart, Qlik still splits by the "
                           "chart's dimensions - add VALUES() of those dimensions if the measure is used there.")
        elif ident not in ("$", ""):
            raise Manual(f"Set identifier '{ident}' (a bookmark or alternate state): no Power BI equivalent in a measure.")
        for m in s.modifiers:
            if not m.field or m.op == "?":
                raise Manual(f"Set modifier '{m.raw}' not understood.")
            table, column = self.field_col(m.field)
            c = col(table, column)
            dtype = self.model.table(table).column(column).data_type
            if m.clear:
                mods.append(f"REMOVEFILTERS({c})")
                continue
            if m.op in ("+=", "/="):
                raise Manual(f"Set modifier '{m.raw}' ({m.op}) combines with the current selection in a way "
                             "CALCULATE cannot express directly.")
            pred = self._elements(m.elements, c, dtype, ctx, m.raw)
            if m.op == "=":
                mods.append(pred)
            elif m.op == "*=":
                mods.append(f"KEEPFILTERS({pred})")
            elif m.op == "-=":
                mods.append(f"KEEPFILTERS(NOT ({pred}))")
        return mods

    def _literal(self, text: str, dtype: str, number: bool) -> str:
        if dtype in ("double", "integer"):
            if number or re.fullmatch(r"-?\d+(\.\d+)?", text):
                return text
            raise Manual(f"Text value '{text}' compared with a numeric field.")
        if dtype == "date":
            raise Manual(f"Date value '{text}' in set analysis: rebuild with DATE() literals.")
        return dstr(text)

    def _elements(self, elements: list[SetElement], c: str, dtype: str, ctx: _Ctx, raw: str) -> str:
        values: list[str] = []
        preds: list[str] = []
        for e in elements:
            if e.kind in ("number", "string"):
                values.append(self._literal(e.text, dtype, e.kind == "number"))
            elif e.kind == "dollar":
                preds.append(f"{c} = {self._dollar(e.text, ctx)}")
            elif e.kind == "search":
                preds.append(self._search(e.text, c, dtype, ctx, raw))
            else:
                raise Manual(f"Set element '{e.text}' in '{raw}' (field reference, P()/E() or expression search).")
        if values:
            preds.insert(0, f"{c} IN {{{', '.join(values)}}}" if len(values) > 1 else f"{c} = {values[0]}")
        if not preds:
            raise Manual(f"Empty element set in '{raw}'.")
        return preds[0] if len(preds) == 1 else "(" + " || ".join(preds) + ")"

    def _search(self, text: str, c: str, dtype: str, ctx: _Ctx, raw: str) -> str:
        t = text.strip()
        if t == "*":
            return f"NOT ISBLANK({c})"
        if t.startswith("$(="):
            return f"{c} = {self._dollar(t[3:-1], ctx)}"
        parts = re.findall(r"(>=|<=|<>|>|<|=)\s*(-?\d+(?:\.\d+)?)", t)
        if parts and re.fullmatch(r"(\s*(>=|<=|<>|>|<|=)\s*-?\d+(\.\d+)?\s*)+", t):
            if dtype not in ("double", "integer"):
                raise Manual(f"Numeric search '{t}' on the text field in '{raw}'.")
            return " && ".join(f"{c} {op} {v}" for op, v in parts)
        if re.fullmatch(r"[^*?]+\*", t):
            ctx.assume(f"Wildcard search '{t}' is case-insensitive in both products; matched with LEFT().")
            return f"LEFT({c}, {len(t) - 1}) = {dstr(t[:-1])}"
        if re.fullmatch(r"\*[^*?]+\*", t):
            ctx.assume(f"Wildcard search '{t}' matched with CONTAINSSTRING (case-insensitive).")
            return f"CONTAINSSTRING({c}, {dstr(t[1:-1])})"
        if re.fullmatch(r"\*[^*?]+", t):
            ctx.assume(f"Wildcard search '{t}' matched with RIGHT().")
            return f"RIGHT({c}, {len(t) - 1}) = {dstr(t[1:])}"
        raise Manual(f"Search string '{t}' in '{raw}' (expression search or '?' wildcards).")

    # ------------------------------------------------------------ scalars

    def _scalar(self, c: Call, sub, ctx: _Ctx) -> str:
        name, n = c.name.lower(), len(c.args)
        if c.set_expr is not None or c.total or c.distinct:
            raise Manual(f"{c.name}() with set analysis or qualifiers is not an aggregation.")
        a = [sub(x) for x in c.args]
        simple = {
            ("if", 3): "IF({0}, {1}, {2})", ("if", 2): "IF({0}, {1})",
            ("isnull", 1): "ISBLANK({0})", ("null", 0): "BLANK()", ("true", 0): "TRUE()", ("false", 0): "FALSE()",
            ("fabs", 1): "ABS({0})", ("sqrt", 1): "SQRT({0})", ("exp", 1): "EXP({0})", ("log", 1): "LN({0})",
            ("log10", 1): "LOG10({0})", ("pow", 2): "POWER({0}, {1})", ("mod", 2): "MOD({0}, {1})",
            ("div", 2): "QUOTIENT({0}, {1})", ("sign", 1): "SIGN({0})",
            ("len", 1): "LEN({0})", ("upper", 1): "UPPER({0})", ("lower", 1): "LOWER({0})", ("trim", 1): "TRIM({0})",
            ("left", 2): "LEFT({0}, {1})", ("right", 2): "RIGHT({0}, {1})", ("mid", 3): "MID({0}, {1}, {2})",
            ("index", 2): "FIND({1}, {0}, 1, 0)", ("replace", 3): "SUBSTITUTE({0}, {1}, {2})",
            ("year", 1): "YEAR({0})", ("month", 1): "MONTH({0})", ("day", 1): "DAY({0})", ("quarter", 1): "QUARTER({0})",
            ("weekday", 1): "WEEKDAY({0}, 3)", ("today", 0): "TODAY()", ("now", 0): "NOW()",
            ("makedate", 3): "DATE({0}, {1}, {2})", ("makedate", 2): "DATE({0}, {1}, 1)", ("makedate", 1): "DATE({0}, 1, 1)",
            ("monthstart", 1): "DATE(YEAR({0}), MONTH({0}), 1)", ("yearstart", 1): "DATE(YEAR({0}), 1, 1)",
            ("addmonths", 2): "EDATE({0}, {1})", ("floor", 1): "FLOOR({0}, 1)", ("ceil", 1): "CEILING({0}, 1)",
            ("text", 1): "({0} & \"\")", ("num", 1): "{0}", ("date", 1): "{0}", ("time", 1): "{0}",
        }
        if (name, n) in simple:
            if name in ("num", "date", "time"):
                ctx.assume(f"{c.name}() is display formatting; the value is kept (set the format on the measure).")
            return simple[(name, n)].format(*a)
        if name in ("num", "date") and n == 2:
            ctx.assume(f"{c.name}() formatting inside the expression is not carried.")
            return a[0]
        if name == "alt" and n >= 2:
            return "COALESCE(" + ", ".join(a) + ")"
        if name == "round":
            if n == 1:
                return f"ROUND({a[0]}, 0)"
            step = c.args[1]
            if isinstance(step, Num):
                v = float(step.value)
                for digits in range(-6, 7):
                    if abs(v - 10 ** (-digits)) < 1e-12:
                        return f"ROUND({a[0]}, {digits})"
            ctx.assume("Round(x, step) became MROUND (ties round away from zero in both).")
            return f"MROUND({a[0]}, {a[1]})"
        if name == "monthend" and n == 1:
            ctx.assume("MonthEnd() returns the last millisecond of the month in Qlik; EOMONTH returns the date.")
            return f"EOMONTH({a[0]}, 0)"
        if name == "week" and n == 1:
            ctx.assume("Week() follows the app's week settings in Qlik; WEEKNUM(..., 21) is ISO weeks.")
            return f"WEEKNUM({a[0]}, 21)"
        if name == "match" and n >= 2:
            ctx.assume("Match() is case-sensitive in Qlik; SWITCH compares text case-insensitively.")
            pairs = ", ".join(f"{v}, {i}" for i, v in enumerate(a[1:], 1))
            return f"SWITCH({a[0]}, {pairs}, 0)"
        if name == "pick" and n >= 2:
            pairs = ", ".join(f"{i}, {v}" for i, v in enumerate(a[1:], 1))
            return f"SWITCH({a[0]}, {pairs})"
        raise Manual(f"{c.name}() has no verified DAX equivalent.")

    # ------------------------------------------------------------ row context

    def _row(self, n: Node, table: str, ctx: _Ctx) -> str:
        if isinstance(n, Num):
            return n.value
        if isinstance(n, Str):
            return dstr(n.value)
        if isinstance(n, FieldRef):
            locs = self.model.locations(n.name)
            here = next((loc for loc in locs if loc[0] == table), None)
            if here:
                return col(*here)
            for t, k in locs:
                if self.model.related_one_side(table, t):
                    return f"RELATED({col(t, k)})"
            raise Manual(f"Field '{n.name}' is not in '{table}' or a table it relates to many-to-one.")
        if isinstance(n, Unary):
            inner = self._row(n.operand, table, ctx)
            return f"NOT ({inner})" if n.op == "not" else f"-({inner})"
        if isinstance(n, Binary):
            return self._binary(n, lambda x: self._row(x, table, ctx))
        if isinstance(n, Call):
            if n.name.lower() in AGG or n.name.lower() == "aggr":
                raise Manual(f"{n.name}() inside a row-level expression.")
            return self._scalar(n, lambda x: self._row(x, table, ctx), ctx)
        if isinstance(n, Dollar):
            raise Manual("$(=...) inside a row-level expression.")
        raise Manual(f"Unsupported row construct {type(n).__name__}.")
