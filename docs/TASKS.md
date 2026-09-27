# Tasks

## Milestone 1: Engine (this branch)
- [x] Loader: unbuild folder/zip, JSON, `.qvs`, `-prj` script; `.qvf`/`.qvw` refused with remedy
- [x] Script splitter and parser (labels, prefixes, preceding loads, all LOAD clauses, model statements)
- [x] Symbolic script execution to M (exact-or-absent), with uniqueness and type evidence
- [x] Associative model → star schema (evidence-based cardinality, synthetic keys, cycles, types, parameters)
- [x] Qlik expression parser (set analysis, qualifiers, dollar expansion)
- [x] Set analysis → DAX with chart context; Aggr, Rank, Column(n), TOTAL, `$(=...)`
- [x] Sheets → pages (grid), filter panes → slicers, 15 chart types, calculated dimensions
- [x] Section access → dynamic RLS
- [x] Assessment, rationalization (incl. unused fields), report, parity queries, CLI
- [x] Synthetic sample app + 66 tests (determinism, no placeholders, refusals)

## Milestone 2: First real app
- [ ] Run `inspect` on the provided app; extend the loader to any unrecognised files
- [ ] AC10: open and refresh the PBIP in Power BI Desktop; pin a regression test for anything Desktop rejects
- [ ] Verify `expressions.tmdl` parameter syntax and many-to-many relationships in Desktop

## Milestone 3: Depth
- [ ] Master calendar pattern → DAX `CALENDAR` table (recognise the standard AUTOGENERATE/IterNo script)
- [ ] FOR EACH file loops → `Folder.Files` + a function query
- [ ] Composite keys for synthetic keys (option) and link-table generation
- [ ] Cyclic groups → field parameters (TMDL `ParameterMetadata`)
- [ ] Alternate states → a disconnected-table pattern
- [ ] QlikView `-prj` XML objects (charts, sheets)
- [ ] Qlik Cloud REST export (spaces, apps) as an optional online extractor
- [ ] Performance test for AC9

## Milestone 4: Platform
- [ ] Fabric outputs (Lakehouse replacing the QVD layer, Direct Lake model)
- [ ] Deployment to a workspace; DashboardBridge UI integration (placeholders off)
