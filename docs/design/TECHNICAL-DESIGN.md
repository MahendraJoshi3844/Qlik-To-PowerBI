# Technical design: qlik2pbi

Implements `docs/specs/SPEC-qlik-to-powerbi-migration.md`. Python ≥ 3.10, standard
library only (the tests use pytest).

## 1. Pipeline (`qlik2pbi/pipeline.py`)

| Stage | Module | Output |
|---|---|---|
| read | `app/loader.py` | `QlikApp`: script, connections, variables, master items, sheets, objects |
| script | `script/statements.py`, `script/interpret.py` | `ScriptModel`: tables with fields, M, evidence, findings |
| assess | `assess/inventory.py`, `assess/rationalize.py` | complexity, effort, readiness; duplicates, unused items and fields |
| model | `semantic/planner.py` | `SemanticModel`: tables, typed columns, relationships, parameters, hierarchies |
| translate | `semantic/measures.py`, `expr/to_dax.py` | master measures, section access role |
| layout | `report/layout.py` | pages, visuals, chart-expression measures, calculated dimensions |
| finalize | `semantic/measures.finalize_measures` | dependency propagation, placeholders, binding pruning |
| emit | `emit/tmdl.py`, `emit/pbir.py`, `emit/pbip.py` | PBIP |
| validate | `validate/checks.py` | structural defects, parity queries |
| report | `output/report.py` | HTML + JSON |

## 2. Reading the load script

**Splitting** (`split_statements`) respects strings, `[brackets]` (including INLINE
blocks), `//` and `/* */` comments, `REM` and `///$tab` markers. Control statements
(IF/FOR/SUB/...) end at the end of the line, as in Qlik.

**Parsing** (`parse_statement`) recognises labels, prefix chains (MAPPING,
CONCATENATE, JOIN/KEEP kinds, CROSSTABLE, FIRST, ...), `LOAD` clauses (field list,
FROM with its format spec, RESIDENT, INLINE, AUTOGENERATE, WHERE/WHILE, GROUP BY),
`SQL SELECT`, and the statements that change the model. A LOAD without a source is
a **preceding load** and is folded onto the next statement.

**Executing** (`Interpreter`) goes top to bottom, as Qlik does:
- `$(v)` expansion is textual and happens per statement, with the variables as they
  stand at that point. LET is evaluated only when constant.
- Each table keeps its field list and an M expression. A statement that changes a
  table rewrites its M: `Table.Combine` for concatenation; `Table.NestedJoin` for
  joins, with keys compared as text and outer-join keys merged as Qlik merges them;
  a semi-join for KEEP; `Table.UnpivotOtherColumns` for CROSSTABLE; `Table.Group`
  for GROUP BY; ApplyMap as a buffered first-match lookup.
- RESIDENT inlines the source table's M *as of that statement*, so later changes to
  the source don't leak backwards.
- Automatic concatenation happens when the field set is identical, as in Qlik.
- **Exact or absent:** any untranslatable step sets `m = None`, with the reason.
  Tables inside FOR/DO loops are absent, and those inside IF blocks are assumed to run.
- Evidence is recorded: `unique` (GROUP BY key), `non_unique` (CROSSTABLE qualifier,
  concatenation), `hints` (types from Year()/arithmetic/etc.).

## 3. Associative model → star schema (`semantic/planner.py`)

- One relationship per pair of tables sharing exactly one field. Two or more
  shared fields form a synthetic key: MANUAL, no relationship.
- The one side needs evidence: proven by GROUP BY, or assumed (the key is the
  table's first field, it's not non-unique, and the table is less fact-like).
  Otherwise the relationship is many-to-many, which never fails a refresh.
- Fact-likeness is the number of aggregations over the table's own fields in
  all measures and charts.
- Cycles: the relationships are sorted (proven, then assumed, then m:m), and a
  union-find marks loop-closing ones inactive.
- Types: one type per field name (so keys match), taken from script hints, then
  date functions, then numeric aggregation or set comparison; otherwise text. Each
  query ends in `Table.TransformColumnTypes`.
- A field's location list puts the one side first (for filters and visual
  axes), then the most fact-like table (for aggregation).

## 4. Expressions → DAX (`expr/to_dax.py`)

The parser (`expr/parser.py`) handles Qlik quoting (`'string'`, `"field"`,
`[field]`), aggregation qualifiers, set expressions, and `$(=...)`. The
translator's pattern table is in the module docstring and in OBJECT-MAPPING.md.
Chart measures get a `ChartContext` (dimensions and sibling measures). Anything
not translated raises `Manual`.

## 5. Front end (`report/layout.py`)

Grid cells are scaled onto 1280×720, and filter panes become stacked slicers. Chart
measures reuse master measures with the same normalised formula, and share one
measure per identical formula (or per formula and dimensions, when the context
matters). Calculated dimensions become calculated columns.

## 6. Security

Section access becomes the role `Section Access`. The hidden security table is
filtered to `USERPRINCIPALNAME()`, and every table holding a reduction field is
filtered to the user's allowed values, or to everything for ADMIN.

## 7. Emission and validation

The PBIR versions are pinned (definition 4.0, report 1.3.0, page 1.0.0,
visualContainer 1.4.0), shared with the sibling products. `lib://` folders become
`expressions.tmdl` query parameters. Structural validation resolves every DAX
reference, relationship, role table and visual binding. Parity queries are
generated per visual.
