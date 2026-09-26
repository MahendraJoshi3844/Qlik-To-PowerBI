# SPEC: Qlik to Power BI migration

Status: Draft v0.1 · Owner: Mahendra Joshi · Date: 2026-09-26 · Technology-agnostic (the how is in the design doc).

## 1. Problem

Organisations leaving Qlik Sense or QlikView for Power BI and Fabric face a
rebuild that the market treats as manual: "complex Qlik scripts cannot be
directly imported into Power BI and need rebuilding", Set Analysis has to be
rewritten as DAX, and the associative model has to become a relational star
schema. Typical programmes run 12-18 months. Three things make Qlik harder
than other sources:

1. **The load script is the model.** There is no model definition to read. Tables,
   joins, lookups, unpivots and staging layers exist only as script statements
   executed in order.
2. **Associations are implicit.** Tables link on every field name they share, with
   no direction or cardinality. Synthetic keys and circular references are normal.
3. **Expressions are selection-aware.** Set analysis, `TOTAL`, `Aggr()` and dollar-sign
   expansion express filter-context logic that DAX expresses differently.

## 2. Goal

Automate the mechanical majority of a Qlik→Power BI migration (script, model,
expressions and sheets) and make the rest visible, estimated and assignable. Every
automated decision must be reviewable, and anything uncertain must say so.

## 3. Users

The migration lead (assessment, sizing, rationalization), BI developers (finishing
and parity), data engineers (sources, QVD replacement, refresh), security admins
(section access to RLS), and business owners (sign-off, retirement).

## 4. Scope (v1)

| Area | In scope |
|---|---|
| Input | `qlik app unbuild` folder (or zip), JSON, `.qvs` script, QlikView `-prj` script; refusal with remedy for `.qvf`/`.qvw` |
| Script | SET/LET and `$(v)` expansion; CONNECT; LOAD from files (csv/txt, xlsx, parquet), SQL SELECT, RESIDENT, INLINE; preceding loads; WHERE; GROUP BY; DISTINCT; FIRST; CROSSTABLE; MAPPING + ApplyMap; CONCATENATE (explicit and automatic); NOCONCATENATE; JOIN/KEEP (inner/left/right/outer); QUALIFY/UNQUALIFY; DROP; RENAME; STORE; section access; control statements, QVD, AUTOGENERATE, INTERVALMATCH, GENERIC, HIERARCHY and BINARY reported |
| Model | Tables, typed columns, relationships from shared fields with evidence-based cardinality, synthetic keys, circular references, drill-down hierarchies, cyclic groups, folder parameters |
| Expressions | Aggregations, set analysis, TOTAL, DISTINCT, Aggr, Rank, Column(n), dollar expansion, scalar functions; inter-record and selection functions reported |
| Front end | Sheets, grid layout, 15 native chart types, filter panes and list boxes, master dimensions and measures, calculated dimensions, alternate states / bookmarks / stories / extensions reported |
| Security | Section access (USERID/NTNAME/USER.EMAIL + reduction fields) to dynamic RLS; OMIT reported |
| Outputs | PBIP, migration report (HTML/JSON), parity DAX queries, service checklist |

Out of scope for v1: direct `.qvf`/`.qvw` reading, QVD data conversion, NPrinting
templates, Fabric-native outputs, deployment, and AI-assisted translation.

## 5. Principles

1. Fidelity over coverage. Exact, assumed with the assumption stated, or not emitted.
2. A table's query is exact or absent.
3. Nothing silently dropped.
4. The output always opens in Power BI Desktop.
5. Offline and metadata-only; deterministic output.

## 6. Acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| AC1 | Unbuild folder, zip, JSON and `.qvs` are read; `.qvf`/`.qvw` are refused with the export command | test_end_to_end |
| AC2 | Each script construct in §4 produces the documented M, or a MANUAL finding naming the statement | test_script |
| AC3 | Relationships follow shared fields; one-to-many only with evidence; synthetic keys are never guessed; at most one active path | test_model |
| AC4 | Set analysis patterns in the mapping matrix translate as documented; unsupported ones are refused | test_to_dax |
| AC5 | Every sheet becomes a page with the grid layout; every visual has a non-empty query, or is reported | test_end_to_end |
| AC6 | Zero dangling references on the sample (structural validation) | test_no_converter_defects |
| AC7 | Identical output for identical input; re-running leaves no stale files | test_deterministic_and_rerunnable |
| AC8 | The report lists every finding, the worklist, rationalization (including unused fields), effort, and the service checklist | test_report_lists_every_finding |
| AC9 | A 5,000-line script and 50 sheets convert in < 30 s | performance test (TASKS) |
| AC10 | The generated PBIP opens and refreshes in Power BI Desktop | **manual**, on the first real app |
