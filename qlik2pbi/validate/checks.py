"""Post-conversion validation.

Two kinds of evidence, because "it converted" is not the same as "it is right":

1. **Structural checks** (run now, offline): every table/column/measure a DAX
   expression, relationship, hierarchy or visual references exists in the model
   that was written. A dangling reference is a converter defect, reported as
   such - Power BI would refuse or break the object.
2. **Data-parity queries** (run by the migration team): for every migrated
   visual, a DAX query returning the same grain and metrics as the
   MicroStrategy grid, to compare against the MicroStrategy report's export.
   Run them in DAX Studio or Power BI Desktop's DAX query view. Running the
   MicroStrategy side and diffing is the reconciliation step of the playbook.
"""

from __future__ import annotations

import re
from pathlib import Path

from qlik2pbi.findings import Fidelity, FindingLog
from qlik2pbi.report.layout import ReportPlan
from qlik2pbi.semantic.model import SemanticModel
from qlik2pbi.emit.pbip import safe_file_name

_COL = re.compile(r"'((?:[^']|'')+)'\[((?:[^\]]|\]\])+)\]")
_MEASURE = re.compile(r"(?<![\w'\]])\[((?:[^\]]|\]\])+)\]")
_STRING = re.compile(r'"(?:[^"]|"")*"')


def _refs(dax: str) -> tuple[list[tuple[str, str]], list[str]]:
    text = _STRING.sub('""', dax)
    cols = [(t.replace("''", "'"), c.replace("]]", "]")) for t, c in _COL.findall(text)]
    stripped = _COL.sub("", text)
    measures = [m.replace("]]", "]") for m in _MEASURE.findall(stripped)]
    return cols, measures


def structural_checks(model: SemanticModel, plan: ReportPlan, log: FindingLog) -> int:
    """Returns the number of defects found."""
    tables = {t.name.lower(): t for t in model.tables}
    measures = {m.name.lower() for m in model.all_measures()}
    defects = 0

    def check(owner: str, dax: str) -> None:
        nonlocal defects
        cols, ms = _refs(dax)
        for t, c in cols:
            table = tables.get(t.lower())
            if table is None or (table.column(c) is None and c.lower() not in measures):
                defects += 1
                log.add("validate", "reference", owner, Fidelity.MANUAL,
                        f"DAX references '{t}'[{c}], which is not in the model (converter defect).",
                        action="Report this to the qlik2pbi team with the input bundle.", source=dax)
        for m in ms:
            if m.lower() not in measures:
                defects += 1
                log.add("validate", "reference", owner, Fidelity.MANUAL,
                        f"DAX references measure [{m}], which is not in the model (converter defect).",
                        action="Report this to the qlik2pbi team with the input bundle.", source=dax)

    for t in model.tables:
        for m in t.measures:
            check(f"measure {m.name}", m.dax)
        for c in t.columns:
            if c.expression:
                check(f"column {t.name}[{c.name}]", c.expression)
    for role in model.roles:
        for table, dax in role.table_filters.items():
            if table.lower() not in tables:
                defects += 1
            check(f"role {role.name}", dax)
    for r in model.relationships:
        for t, c in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
            table = tables.get(t.lower())
            if table is None or table.column(c) is None:
                defects += 1
                log.add("validate", "relationship", f"{r.from_table} -> {r.to_table}", Fidelity.MANUAL,
                        f"Relationship column '{t}'[{c}] is not in the model (converter defect).")
    for page in plan.pages:
        for v in page.visuals:
            for f in v.fields():
                table = tables.get(f.table.lower())
                ok = table is not None and (
                    (f.is_measure and f.column.lower() in measures) or (not f.is_measure and table.column(f.column))
                )
                if not ok:
                    defects += 1
                    log.add("validate", "visual", f"{page.name} / {v.name}", Fidelity.MANUAL,
                            f"Visual binds '{f.table}'[{f.column}], which is not in the model (converter defect).")
    return defects


def _q(table: str, col: str) -> str:
    return "'" + table.replace("'", "''") + "'[" + col.replace("]", "]]") + "]"


def parity_queries(plan: ReportPlan, out_dir: Path) -> list[Path]:
    """One .dax file per migrated visual that has at least one measure."""
    written: list[Path] = []
    folder = out_dir / "validation" / "parity-queries"
    for page in plan.pages:
        for v in page.visuals:
            if v.visual_type == "slicer":
                continue
            fields = v.fields()
            groups = list(dict.fromkeys((f.table, f.column) for f in fields if not f.is_measure))
            ms = list(dict.fromkeys(f.column for f in fields if f.is_measure))
            if not ms:
                continue
            args = [_q(t, c) for t, c in groups] + [f'"{m}", [{m}]' for m in ms]
            body = ",\n    ".join(args)
            order = ", ".join(_q(t, c) for t, c in groups)
            text = (
                f"// Parity check for: {page.origin} / {page.name} / {v.name}\n"
                "// Run in DAX Studio or Power BI Desktop (DAX query view) and compare with the\n"
                "// MicroStrategy grid exported at the same attributes and filters.\n"
                "EVALUATE\nSUMMARIZECOLUMNS(\n    " + body + "\n)\n"
                + (f"ORDER BY {order}\n" if order else "")
            )
            path = folder / f"{safe_file_name(page.name, 'page')}__{safe_file_name(v.name, 'visual')}.dax"
            n = 2
            while path in written:
                path = path.with_name(f"{path.stem}_{n}.dax")
                n += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    return written
