"""The planned Power BI semantic model (target side), before it is written as TMDL."""

from __future__ import annotations

from dataclasses import dataclass, field

from qlik2pbi.findings import Fidelity


@dataclass
class SemColumn:
    name: str
    data_type: str = "string"  # string | integer | double | date | datetime | boolean
    source_column: str = ""
    expression: str = ""  # DAX, for calculated columns
    hidden: bool = False
    is_key: bool = False
    format_string: str = ""
    field: str = ""  # the Qlik field this column realises


@dataclass
class SemMeasure:
    name: str
    dax: str
    format_string: str = ""
    folder: str = ""
    description: str = ""
    fidelity: Fidelity = Fidelity.EXACT
    source: str = ""  # original Qlik expression
    notes: list[str] = field(default_factory=list)
    origin: str = "master"  # master | chart:<sheet>
    depends_on: set[str] = field(default_factory=set)


@dataclass
class SemHierarchy:
    name: str
    levels: list[str]


@dataclass
class SemTable:
    name: str
    kind: str  # data | measures | security
    columns: list[SemColumn] = field(default_factory=list)
    measures: list[SemMeasure] = field(default_factory=list)
    hierarchies: list[SemHierarchy] = field(default_factory=list)
    partition_m: str = ""
    hidden: bool = False
    description: str = ""
    source_table: str = ""  # the Qlik script table
    has_query: bool = True  # False when written with an empty typed query

    def column(self, name: str) -> SemColumn | None:
        low = name.lower()
        return next((c for c in self.columns if c.name.lower() == low), None)


@dataclass
class SemRelationship:
    from_table: str  # many side
    from_column: str
    to_table: str  # one side (or the other many side)
    to_column: str
    active: bool = True
    many_to_many: bool = False
    reason: str = ""


@dataclass
class SemRole:
    name: str
    table_filters: dict[str, str] = field(default_factory=dict)
    members: list[str] = field(default_factory=list)
    fidelity: Fidelity = Fidelity.EXACT
    source: str = ""


@dataclass
class SemParameter:
    """A shared M expression (a query parameter), e.g. the folder behind lib://DataFiles."""

    name: str
    value: str
    description: str = ""


@dataclass
class SemanticModel:
    name: str
    tables: list[SemTable] = field(default_factory=list)
    relationships: list[SemRelationship] = field(default_factory=list)
    roles: list[SemRole] = field(default_factory=list)
    parameters: list[SemParameter] = field(default_factory=list)
    culture: str = "en-US"
    #: Qlik field name (lower) -> [(table, column)], the one-side table first.
    field_map: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: Qlik table name -> fact score (numeric aggregations over its own fields).
    fact_score: dict[str, int] = field(default_factory=dict)

    MEASURES_TABLE = "Measures"

    def table(self, name: str) -> SemTable | None:
        low = name.lower()
        return next((t for t in self.tables if t.name.lower() == low), None)

    def measures_table(self) -> SemTable:
        t = self.table(self.MEASURES_TABLE)
        if t is None:
            t = SemTable(
                name=self.MEASURES_TABLE, kind="measures",
                columns=[SemColumn(name="_", data_type="string", source_column="_", hidden=True)],
                partition_m='let Source = #table(type table [#"_" = text], {}) in Source',
                description="Home table for migrated Qlik master measures and chart expressions.",
            )
            self.tables.append(t)
        return t

    def all_measures(self) -> list[SemMeasure]:
        return [m for t in self.tables for m in t.measures]

    def measure(self, name: str) -> SemMeasure | None:
        low = name.lower()
        return next((m for m in self.all_measures() if m.name.lower() == low), None)

    def locations(self, field_name: str) -> list[tuple[str, str]]:
        return self.field_map.get(field_name.lower(), [])

    def related_one_side(self, many: str, one: str) -> bool:
        """Is `one` reachable from `many` along active many-to-one relationships?"""
        seen, frontier = {many.lower()}, [many.lower()]
        while frontier:
            cur = frontier.pop()
            for r in self.relationships:
                if r.active and not r.many_to_many and r.from_table.lower() == cur:
                    nxt = r.to_table.lower()
                    if nxt == one.lower():
                        return True
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
        return False
