"""The Qlik app as read: script, connections, master items, variables, sheets, objects.

This is the *source* side. It holds what the app says, not what Power BI will
get; the load script is kept as text here and parsed by `script/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Connection:
    name: str
    kind: str = ""  # folder | sqlserver | odbc | oledb | snowflake | rest | ...
    connection_string: str = ""
    path: str = ""  # for folder (lib://) connections


@dataclass
class Variable:
    name: str
    definition: str
    comment: str = ""


@dataclass
class MasterDimension:
    id: str
    title: str
    fields: list[str] = field(default_factory=list)  # field names or =expressions
    grouping: str = "N"  # N single | H drill-down | C cyclic
    description: str = ""


@dataclass
class MasterMeasure:
    id: str
    title: str
    expression: str
    label: str = ""
    number_format: str = ""
    description: str = ""


@dataclass
class ChartDimension:
    field: str  # field name, or an expression starting with '='
    label: str = ""
    library_id: str = ""


@dataclass
class ChartMeasure:
    expression: str
    label: str = ""
    library_id: str = ""
    number_format: str = ""


@dataclass
class QlikObject:
    """A visualization (or container) on a sheet."""

    id: str
    qtype: str  # barchart, linechart, table, pivot-table, kpi, filterpane, listbox, ...
    title: str = ""
    dimensions: list[ChartDimension] = field(default_factory=list)
    measures: list[ChartMeasure] = field(default_factory=list)
    options: dict = field(default_factory=dict)  # orientation, barGrouping, lineType, ...
    children: list[str] = field(default_factory=list)  # e.g. listboxes of a filterpane
    state: str = ""  # alternate state name, if any
    text: str = ""


@dataclass
class Cell:
    object_id: str
    col: float = 0
    row: float = 0
    colspan: float = 0
    rowspan: float = 0


@dataclass
class Sheet:
    id: str
    title: str
    cells: list[Cell] = field(default_factory=list)
    rank: float = 0


@dataclass
class QlikApp:
    name: str = "App"
    source_kind: str = "unbuild"  # unbuild | script | json | prj
    script: str = ""
    connections: list[Connection] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    dimensions: list[MasterDimension] = field(default_factory=list)
    measures: list[MasterMeasure] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)
    objects: dict[str, QlikObject] = field(default_factory=dict)
    bookmarks: list[str] = field(default_factory=list)
    alternate_states: list[str] = field(default_factory=list)
    stories: list[str] = field(default_factory=list)
    #: Anything seen but not understood, listed by `inspect`.
    unrecognized: list[str] = field(default_factory=list)

    @staticmethod
    def _find(items, name: str, attr: str):
        low = name.strip().lower()
        for it in items:
            if getattr(it, attr).lower() == low:
                return it
        return None

    def variable(self, name: str) -> Variable | None:
        return self._find(self.variables, name, "name")

    def connection(self, name: str) -> Connection | None:
        return self._find(self.connections, name, "name")

    def master_measure(self, key: str) -> MasterMeasure | None:
        return next((m for m in self.measures if m.id == key), None) or self._find(self.measures, key, "title")

    def master_dimension(self, key: str) -> MasterDimension | None:
        return next((d for d in self.dimensions if d.id == key), None) or self._find(self.dimensions, key, "title")

    def counts(self) -> dict[str, int]:
        charts = [o for o in self.objects.values() if o.qtype not in ("sheet", "listbox")]
        return {
            "script_lines": len(self.script.splitlines()),
            "connections": len(self.connections),
            "variables": len(self.variables),
            "master_dimensions": len(self.dimensions),
            "master_measures": len(self.measures),
            "sheets": len(self.sheets),
            "objects": len(charts),
            "bookmarks": len(self.bookmarks),
            "alternate_states": len(self.alternate_states),
            "stories": len(self.stories),
        }
