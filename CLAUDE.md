# CLAUDE.md

Guidance for Claude Code in this repository.

## Project

**qlik2pbi** migrates **Qlik Sense / QlikView** apps to **Power BI** (PBIP: TMDL + PBIR)
and produces assessment, rationalization and migration reports. It is a sibling of
t2pbi (Tableau) and mstr2pbi (MicroStrategy) but built around what is specific to
Qlik: the **load script is the data model**, associations are **by field name**, and
calculations are **set analysis**. Do not port code from the siblings except the
proven PBIR/TMDL writer conventions (pinned schema versions).

Docs: `docs/specs/SPEC-qlik-to-powerbi-migration.md`, `docs/design/TECHNICAL-DESIGN.md`,
`docs/design/OBJECT-MAPPING.md`, `docs/design/INPUT-FORMATS.md`,
`docs/playbook/MIGRATION-PLAYBOOK.md`, `docs/TASKS.md`.

## Architecture

`read → script → assess → model → translate → layout → finalize → emit → validate → report`

- `qlik2pbi/pipeline.py` is the **only** module that knows the order.
- `app/` = what the Qlik app says. `script/interpret.py` executes the load script
  symbolically into tables + M. `semantic/` plans the target model; `report/` the pages.
- Every non-exact outcome is a `Finding` with a fidelity tier.

## Commands

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"
qlik2pbi inspect <input>
qlik2pbi convert <input> --out out/ [--no-placeholders]
pytest
```

## Critical rules

- **Trust contract.** DAX and M are EXACT, ASSUMED (assumption written down), or not
  emitted. A table's M is exact or absent - never a query that loads different rows.
- **Nothing silently dropped**: every object not converted has a finding.
- **Offline, metadata only**: no network, never read QVDs or app data.
- **Deterministic output** (tested). No timestamps.
- **Standard library only** in `qlik2pbi/`.
- **PBIR schema versions are pinned** in `emit/pbir.py`.
- **Customer apps never enter git** (`customer/` is ignored; `samples/` is synthetic).

## Extending

- Script construct → `script/statements.py` (parse) + `script/interpret.py` (execute) + a test in `tests/test_script.py`.
- Script function → `expr/to_m.py`. Chart function / set analysis → `expr/to_dax.py` + a row in `tests/test_to_dax.py`.
- Visualization → `VISUALS` / `MANUAL_VISUALS` in `report/layout.py`.
- Update `docs/design/OBJECT-MAPPING.md` with every mapping change.

## Workflow

One feature per branch (never commit to `main` directly); after pulling, branch then
push. Spec-Driven Development: spec → design → tasks → build → validate against the
spec's acceptance criteria. Use `.claude/agents/` for isolated work.
