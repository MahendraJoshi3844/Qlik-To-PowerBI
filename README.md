# qlik2pbi: Qlik Sense / QlikView to Power BI migration accelerator

qlik2pbi converts a Qlik app into a Power BI project (PBIP) that opens in Power
BI Desktop. It covers the three things that make Qlik migrations expensive:

1. **The load script becomes Power Query M.** The script is executed
   symbolically, statement by statement. The result includes resident and
   preceding loads, CONCATENATE (explicit and automatic), JOIN/KEEP of every
   kind, ApplyMap, CROSSTABLE, GROUP BY, QUALIFY, DROP, RENAME, variables, and
   `lib://` folders as query parameters.
2. **The associative model becomes a star schema.** Shared field names become
   relationships. Cardinality is set only with evidence (otherwise
   many-to-many), synthetic keys are reported, circular references become
   inactive relationships, and types are inferred from how fields are used.
3. **Set analysis becomes DAX.** Modifiers, `{1}`, `TOTAL`, `DISTINCT`,
   `$(variable)` and `$(=Max(Year))` expansion, Aggr() iterators, and chart
   context for `Rank()` and `Column(n)`.

Sheets become report pages (the Qlik grid layout is kept), filter panes become
slicers, master items become measures and hierarchies, and section access
becomes a dynamic RLS role. Everything else goes on a worklist with the Power BI
construct to use. The run also produces an assessment (complexity, effort,
readiness) and a rationalization plan, including the fields your app loads but
never uses.

```
unbuild folder ─► read ─► script ─► assess ─► model ─► translate ─► layout ─► emit ─► validate ─► report
                  app     M per     effort    star     set analysis  sheets    PBIP    parity DAX   HTML/JSON
                          table     + unused  schema   → DAX, RLS    → pages
```

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

qlik2pbi inspect samples/sales_app                   # what is in it, how the script reads, table by table
qlik2pbi assess  samples/sales_app --out out/assess  # complexity, effort, rationalization
qlik2pbi convert samples/sales_app --out out/sales   # → out/sales/Sales Analytics.pbip + migration_report.html
pytest                                               # 66 tests
```

## Getting the app out of Qlik

A `.qvf` or `.qvw` is a proprietary binary container, so it is not read
directly. Export it instead:

* **Qlik Sense:** `qlik app unbuild --app <app-id> --dir out/` (qlik-cli). Convert
  the folder, or zip it.
* **QlikView:** enable the `-prj` folder in Document Properties and save. Only
  the load script is read from it today.
* **Only the script:** a `.qvs` file converts the data model alone.

The app's data is never read: conversion works from metadata and script text
only, offline. See [docs/design/INPUT-FORMATS.md](docs/design/INPUT-FORMATS.md).

## The trust contract

| Tier | Meaning |
|---|---|
| **exact** | Same result as Qlik |
| **assumed** | Converted; relies on a stated assumption (written into the finding and the measure description) |
| **manual** | Not converted. A table keeps its columns with an empty query; an expression becomes a `BLANK()` placeholder, or is omitted with `--no-placeholders`. The worklist says what to build |

A table's query is **exact or absent**. If any statement that shaped a table
can't be expressed faithfully (a QVD source, a FOR loop, an untranslatable
WHERE), the table gets no query rather than a query returning different rows.

## Documentation

* Spec: [docs/specs/SPEC-qlik-to-powerbi-migration.md](docs/specs/SPEC-qlik-to-powerbi-migration.md)
* Technical design: [docs/design/TECHNICAL-DESIGN.md](docs/design/TECHNICAL-DESIGN.md)
* Mapping matrix: [docs/design/OBJECT-MAPPING.md](docs/design/OBJECT-MAPPING.md)
* Input formats: [docs/design/INPUT-FORMATS.md](docs/design/INPUT-FORMATS.md)
* Delivery playbook: [docs/playbook/MIGRATION-PLAYBOOK.md](docs/playbook/MIGRATION-PLAYBOOK.md)
* Tasks: [docs/TASKS.md](docs/TASKS.md)

`samples/sales_app` is **synthetic** (see `samples/generate_sales_app.py`). Real
apps go in `customer/`, which git ignores.
