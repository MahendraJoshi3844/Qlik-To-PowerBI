"""Master measures -> DAX measures; section access -> a dynamic RLS role.

Chart expressions are translated in the layout stage (report/layout.py), because
their meaning can depend on the chart they sit in; both paths share `ToDax` and
the dependency propagation below.
"""

from __future__ import annotations

import re

from qlik2pbi.app.model import QlikApp
from qlik2pbi.expr.to_dax import Manual, ToDax, convert_format, col, dstr
from qlik2pbi.findings import Fidelity, FindingLog
from qlik2pbi.script.interpret import ScriptModel
from qlik2pbi.semantic.model import SemanticModel, SemMeasure, SemRole

PLACEHOLDER = "BLANK()"


def normalize(expr: str) -> str:
    return re.sub(r"\s+", "", expr.lstrip("=")).lower()


def variables_of(app: QlikApp, sm: ScriptModel) -> dict[str, str]:
    out = dict(sm.variables)
    out.update({v.name: v.definition for v in app.variables})
    return out


def measure_names(app: QlikApp, model: SemanticModel, log: FindingLog) -> dict[str, str]:
    """Master measure title (lower) -> a name unique model-wide and never a column's."""
    taken = {c.name.lower() for t in model.tables for c in t.columns}
    used: set[str] = set()
    out: dict[str, str] = {}
    for m in app.measures:
        name = m.title or m.label or m.id
        if name.lower() in taken:
            name = f"{name} (measure)"
            log.add("translate", "master measure", m.title, Fidelity.INFO, f"Renamed to '{name}': a field has this name.")
        while name.lower() in used:
            name += "_"
        used.add(name.lower())
        out[m.title.lower()] = name
    return out


def translator(app: QlikApp, sm: ScriptModel, model: SemanticModel, names: dict[str, str]) -> ToDax:
    return ToDax(model, variables_of(app, sm), names)


def translate_master_measures(app: QlikApp, model: SemanticModel, tr: ToDax, names: dict[str, str],
                              log: FindingLog) -> None:
    home = model.measures_table()
    for m in app.measures:
        final = names[m.title.lower()]
        fmt = convert_format(m.number_format)
        try:
            res = tr.measure(m.expression)
        except Manual as exc:
            home.measures.append(SemMeasure(final, PLACEHOLDER, fmt, "Master measures", m.description,
                                            Fidelity.MANUAL, m.expression, [str(exc)], "master"))
            continue
        home.measures.append(SemMeasure(final, res.dax, res.format_string or fmt, "Master measures", m.description,
                                        res.fidelity, m.expression, list(res.notes), "master", set(res.depends_on)))


def finalize_measures(model: SemanticModel, log: FindingLog, placeholders: bool) -> set[str]:
    """Propagate MANUAL/ASSUMED through dependencies, drop placeholders if asked, log.

    Returns the names of measures that were withheld (not written)."""
    home = model.measures_table()
    by_name = {m.name: m for m in home.measures}
    changed = True
    while changed:
        changed = False
        for ms in home.measures:
            broken = sorted(d for d in ms.depends_on if d in by_name and by_name[d].fidelity is Fidelity.MANUAL)
            if broken and ms.fidelity is not Fidelity.MANUAL:
                ms.fidelity = Fidelity.MANUAL
                ms.notes.append("Depends on untranslated measure(s): " + ", ".join(broken) + ".")
                changed = True
                continue
            assumed = sorted(d for d in ms.depends_on if d in by_name and by_name[d].fidelity is Fidelity.ASSUMED)
            if assumed and ms.fidelity is Fidelity.EXACT:
                ms.fidelity = Fidelity.ASSUMED
                ms.notes.append("Inherits the assumptions of: " + ", ".join(assumed) + ".")
                changed = True
    withheld: set[str] = set()
    if not placeholders:
        withheld = {m.name for m in home.measures if m.dax == PLACEHOLDER}
        grew = True
        while grew:
            grew = False
            for m in home.measures:
                if m.name not in withheld and m.depends_on & withheld:
                    withheld.add(m.name)
                    grew = True
        for m in home.measures:
            if m.name in withheld:
                m.notes.append("Not emitted: nothing is written for an expression that cannot be translated.")
    for m in home.measures:
        kind = "master measure" if m.origin == "master" else "chart expression"
        if m.name in withheld:
            action = ("Write this measure by hand in Power BI." if m.dax == PLACEHOLDER
                      else f"Add it once its dependencies exist; its translated DAX is: {m.dax}")
        else:
            action = {
                Fidelity.MANUAL: "Write the DAX by hand; the measure is a BLANK() placeholder until then."
                if m.dax == PLACEHOLDER else "Translate the measures it depends on; its own DAX is ready.",
                Fidelity.ASSUMED: "Confirm the stated assumption holds for this app.",
            }.get(m.fidelity, "")
        log.add("translate", kind, m.name, m.fidelity, " ".join(m.notes) if m.notes else "Translated.",
                action=action, source=m.source)
    home.measures = [m for m in home.measures if m.name not in withheld]
    return withheld


_IDENTITY = ("USER.EMAIL", "USERID", "NTNAME", "NTDOMAINSID", "NTSID")
_RESERVED = {"ACCESS", "PASSWORD", "SERIAL", "OMIT", "GROUP", *_IDENTITY}


def translate_section_access(model: SemanticModel, log: FindingLog) -> None:
    sec = next((t for t in model.tables if t.kind == "security"), None)
    if sec is None:
        return
    cols = {c.name.upper(): c.name for c in sec.columns}
    ident = next((cols[i] for i in _IDENTITY if i in cols), None)
    if ident is None:
        log.add("translate", "section access", sec.name, Fidelity.MANUAL,
                "Section access without a USERID / NTNAME / USER.EMAIL column.",
                action="Build the RLS role by hand.")
        return
    if "OMIT" in cols:
        log.add("translate", "section access", "OMIT", Fidelity.MANUAL,
                "OMIT hides fields per user: that is object-level security in Power BI.",
                action="Define object-level security (Tabular Editor) for the omitted fields.")
    reductions = [c for up, c in cols.items() if up not in _RESERVED]
    user = f"{col(sec.name, ident)} = USERPRINCIPALNAME()"
    admin = (f"NOT ISEMPTY(CALCULATETABLE({_q(sec.name)}, {user}, {col(sec.name, cols['ACCESS'])} = \"ADMIN\"))"
             if "ACCESS" in cols else "FALSE()")
    role = SemRole(name="Section Access", fidelity=Fidelity.ASSUMED, source="Section Access table")
    role.table_filters[sec.name] = user
    notes = [f"Users are matched on '{ident}' against USERPRINCIPALNAME(): replace the Qlik identities "
             "(e.g. DOMAIN\\user) with Entra ID user principal names in the section access source.",
             "Reduction values are compared case-insensitively (DAX); Qlik compares them exactly."]
    for r in reductions:
        locs = [loc for f, locs in model.field_map.items() if f.upper() == r.upper() for loc in locs]
        if not locs:
            log.add("translate", "section access", r, Fidelity.MANUAL,
                    f"Reduction field '{r}' matches no field of the data model.",
                    action="Check the field name (Qlik requires the data field in upper case too).")
            continue
        # Qlik reduces every table holding the field; relationships alone would
        # not carry the filter across many-to-many or inactive links.
        allowed = f"CALCULATETABLE(VALUES({col(sec.name, r)}), {user})"
        for t, c in locs:
            pred = f"{col(t, c)} IN {allowed} || {admin}"
            existing = role.table_filters.get(t)
            role.table_filters[t] = f"({existing}) && ({pred})" if existing else pred
    if len(reductions) > 1:
        notes.append("Several reduction fields are applied independently; Qlik applies each section access row as "
                     "a combination.")
    model.roles.append(role)
    log.add("translate", "section access", "Section Access", Fidelity.ASSUMED,
            "Became the dynamic RLS role 'Section Access' (rows of the security table per signed-in user). "
            + " ".join(notes),
            action="Add every user (or an Entra ID group) to the role in the Power BI Service and test with "
                   "'View as role'.")


def _q(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


__all__ = ["PLACEHOLDER", "normalize", "measure_names", "translator", "translate_master_measures",
           "finalize_measures", "translate_section_access", "dstr"]
