"""SemanticModel -> TMDL files (deterministic)."""

from __future__ import annotations

import hashlib

from qlik2pbi.expr.to_m import m_string
from qlik2pbi.findings import Fidelity
from qlik2pbi.semantic.model import SemanticModel, SemColumn, SemMeasure, SemParameter, SemRole, SemTable

_TYPES = {
    "string": "string",
    "integer": "int64",
    "double": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "boolean": "boolean",
}


def tmdl_type(dt: str) -> str:
    return _TYPES.get(dt, "string")


def q(name: str) -> str:
    """Quote a TMDL object name (always quoted; embedded quotes doubled)."""
    clean = name.replace("\r", " ").replace("\n", " ")
    return "'" + clean.replace("'", "''") + "'"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _description(lines: list[str], indent: str, text: str) -> None:
    for part in text.strip().splitlines():
        lines.append(f"{indent}/// {part.strip()}")


def _multiline(lines: list[str], indent: str, prop: str, body: str) -> None:
    lines.append(f"{indent}{prop} =")
    for row in body.splitlines():
        lines.append(f"{indent}\t\t{row}" if row.strip() else "")


def _column(c: SemColumn) -> list[str]:
    out: list[str] = []
    head = f"\tcolumn {q(c.name)}"
    if c.expression:
        head += f" = {_one_line(c.expression)}"
    out.append(head)
    out.append(f"\t\tdataType: {tmdl_type(c.data_type)}")
    if c.is_key:
        out.append("\t\tisKey")
    if c.hidden:
        out.append("\t\tisHidden")
    if c.format_string:
        out.append(f"\t\tformatString: {c.format_string}")
    elif c.data_type == "date":
        out.append("\t\tformatString: yyyy-mm-dd")
    out.append("\t\tsummarizeBy: none")
    if c.source_column:
        out.append(f"\t\tsourceColumn: {c.source_column}")
    if c.field:
        out.append("")
        out.append(f"\t\tannotation QLIK_Field = {_one_line(c.field)}")
    out.append("")
    return out


def _measure(m: SemMeasure) -> list[str]:
    out: list[str] = []
    desc = m.description
    if m.fidelity in (Fidelity.MANUAL, Fidelity.UNSUPPORTED):
        desc = ("MIGRATION TODO: " + " ".join(m.notes) + ("\n" + desc if desc else "")).strip()
    elif m.fidelity is Fidelity.ASSUMED and m.notes:
        desc = ("MIGRATION ASSUMPTION: " + " ".join(m.notes) + ("\n" + desc if desc else "")).strip()
    if desc:
        _description(out, "\t", desc)
    out.append(f"\tmeasure {q(m.name)} = {_one_line(m.dax)}")
    if m.format_string:
        out.append(f"\t\tformatString: {m.format_string}")
    if m.folder:
        out.append(f"\t\tdisplayFolder: {m.folder}")
    out.append("")
    out.append(f"\t\tannotation QLIK_Fidelity = {m.fidelity.value}")
    if m.source:
        out.append("")
        out.append(f"\t\tannotation QLIK_Expression = {_one_line(m.source)}")
    out.append("")
    return out


def table_tmdl(t: SemTable) -> str:
    lines: list[str] = []
    if t.description:
        _description(lines, "", t.description)
    lines.append(f"table {q(t.name)}")
    if t.hidden:
        lines.append("\tisHidden")
    lines.append("")
    for m in sorted(t.measures, key=lambda x: x.name.lower()):
        lines.extend(_measure(m))
    for c in t.columns:
        lines.extend(_column(c))
    for h in t.hierarchies:
        lines.append(f"\thierarchy {q(h.name)}")
        lines.append("")
        for level in h.levels:
            lines.append(f"\t\tlevel {q(level)}")
            lines.append(f"\t\t\tcolumn: {q(level)}")
            lines.append("")
    lines.append(f"\tpartition {q(t.name)} = m")
    lines.append("\t\tmode: import")
    _multiline(lines, "\t\t", "source", t.partition_m)
    lines.append("")
    if t.source_table:
        lines.append(f"\tannotation QLIK_ScriptTable = {_one_line(t.source_table)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def expressions_tmdl(params: list[SemParameter]) -> str:
    """Shared M expressions: the query parameters standing in for lib:// folders."""
    blocks = []
    for p in sorted(params, key=lambda x: x.name.lower()):
        block = []
        if p.description:
            block.append(f"/// {p.description}")
        block.append(f"expression {q(p.name)} = {m_string(p.value)} meta "
                     "[IsParameterQuery = true, Type = \"Text\", IsParameterQueryRequired = true]")
        blocks.append("\n".join(block))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def model_tmdl(model: SemanticModel) -> str:
    lines = [
        "model Model",
        f"\tculture: {model.culture}",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tdiscourageImplicitMeasures",
        "",
        "\tannotation QLIK_MigratedBy = qlik2pbi",
        "",
    ]
    for t in sorted(model.tables, key=lambda x: x.name.lower()):
        lines.append(f"ref table {q(t.name)}")
    return "\n".join(lines).rstrip() + "\n"


def rel_id(from_table: str, from_col: str, to_table: str, to_col: str) -> str:
    h = hashlib.sha1(f"{from_table}|{from_col}|{to_table}|{to_col}".encode("utf-8")).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def relationships_tmdl(model: SemanticModel) -> str:
    blocks = []
    for r in sorted(model.relationships, key=lambda r: (r.from_table.lower(), r.from_column.lower(), r.to_table.lower())):
        b = [
            f"relationship {rel_id(r.from_table, r.from_column, r.to_table, r.to_column)}",
            f"\tfromColumn: {q(r.from_table)}.{q(r.from_column)}",
            f"\ttoColumn: {q(r.to_table)}.{q(r.to_column)}",
        ]
        if not r.active:
            b.append("\tisActive: false")
        if r.many_to_many:
            b.append("\tfromCardinality: many")
            b.append("\ttoCardinality: many")
        blocks.append("\n".join(b))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def role_tmdl(role: SemRole) -> str:
    lines = [f"role {q(role.name)}", "\tmodelPermission: read", ""]
    for table in sorted(role.table_filters):
        lines.append(f"\ttablePermission {q(table)} = {_one_line(role.table_filters[table])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
