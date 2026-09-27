"""Rationalization: migrate less, and load less.

Qlik apps routinely load every column of every source "just in case", because
the associative engine compresses them well. In an Import-mode Power BI model
each unused column costs refresh time and memory, so the most valuable
recommendation here is often the list of loaded-but-never-used fields.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from qlik2pbi.app.model import QlikApp
from qlik2pbi.expr.ast import FieldRef, walk
from qlik2pbi.expr.parser import ParseError, expand, parse
from qlik2pbi.script.interpret import ScriptModel


@dataclass
class Rationalization:
    duplicate_master_measures: list[list[str]] = field(default_factory=list)
    chart_expressions_duplicating_masters: list[dict] = field(default_factory=list)
    unused_master_measures: list[str] = field(default_factory=list)
    unused_master_dimensions: list[str] = field(default_factory=list)
    unused_fields: dict[str, list[str]] = field(default_factory=dict)
    unused_variables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr.lstrip("=")).lower()


def _fields(text: str, variables: dict[str, str]) -> set[str]:
    try:
        return {n.name for n in walk(parse(expand(text, variables)[0])) if isinstance(n, FieldRef)}
    except ParseError:
        return set(re.findall(r"\[([^\]]+)\]", text))


def rationalize(app: QlikApp, sm: ScriptModel) -> Rationalization:
    out = Rationalization()
    variables = dict(sm.variables)
    variables.update({v.name: v.definition for v in app.variables})

    groups: dict[str, list[str]] = {}
    for m in app.measures:
        groups.setdefault(_norm(m.expression), []).append(m.title)
    out.duplicate_master_measures = sorted(sorted(g) for g in groups.values() if len(g) > 1)

    used_measures: set[str] = set()
    used_dims: set[str] = set()
    used_fields: set[str] = set()
    texts: list[str] = []
    for obj in app.objects.values():
        for m in obj.measures:
            if m.library_id:
                mm = app.master_measure(m.library_id)
                if mm:
                    used_measures.add(mm.title)
            elif m.expression:
                texts.append(m.expression)
                master = next((x.title for x in app.measures if _norm(x.expression) == _norm(m.expression)), None)
                if master:
                    out.chart_expressions_duplicating_masters.append(
                        {"object": obj.title or obj.id, "expression": m.expression, "master": master})
        for d in obj.dimensions:
            if d.library_id:
                md = app.master_dimension(d.library_id)
                if md:
                    used_dims.add(md.title)
                    used_fields.update(f for f in md.fields if not f.startswith("="))
            elif d.field:
                if d.field.startswith("="):
                    texts.append(d.field)
                else:
                    used_fields.add(d.field)
    # Master measures referenced by other master measures' labels count as used.
    for m in app.measures:
        for other in app.measures:
            if other is not m and f"[{m.title.lower()}]" in other.expression.lower():
                used_measures.add(m.title)
    texts += [m.expression for m in app.measures if m.title in used_measures]
    for t in texts:
        used_fields |= _fields(t, variables)
    out.unused_master_measures = sorted(m.title for m in app.measures if m.title not in used_measures)
    out.unused_master_dimensions = sorted(d.title for d in app.dimensions if d.title not in used_dims)

    loaded = [t for t in sm.loaded() if not t.section_access]
    counts: dict[str, int] = {}
    for t in loaded:
        for f in t.fields:
            counts[f] = counts.get(f, 0) + 1
    keys = {f for f, n in counts.items() if n > 1}
    reduction = {f.upper() for t in sm.loaded() if t.section_access for f in t.fields}
    for t in loaded:
        unused = [f for f in t.fields if f not in used_fields and f not in keys and f.upper() not in reduction]
        if unused:
            out.unused_fields[t.name] = unused
    all_text = app.script + " ".join(m.expression for m in app.measures) + " ".join(
        m.expression for o in app.objects.values() for m in o.measures)
    out.unused_variables = sorted(v.name for v in app.variables if f"$({v.name.lower()}" not in all_text.lower())
    return out
