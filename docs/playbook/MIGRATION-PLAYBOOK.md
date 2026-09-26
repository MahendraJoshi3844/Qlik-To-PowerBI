# Qlik → Power BI migration playbook

## Phase 0: Export (week 0)
- Inventory the apps (QMC, or the Qlik Cloud catalog) along with their usage (App Metadata Analyzer /
  Operations Monitor: sessions per app for the last 90 days).
- `qlik app unbuild` for every app in scope, into `customer/<app>/`. Export QlikView
  documents as `-prj` folders.
- Run `qlik2pbi inspect` on each: nothing should be listed as unrecognised.

## Phase 1: Assess (week 1)
- `qlik2pbi assess` per app. Read the complexity of the script, the sheets and the
  expressions, the effort estimate, and the readiness percentage.
- Pick a pilot app of medium complexity that the business values.
- **Decision:** scope, waves, budget.

## Phase 2: Rationalize (week 1-2)
- Retire apps with no sessions. Merge duplicate master measures. Promote inline
  chart expressions to master items.
- **Load less:** remove the unused fields listed in the report from the scripts
  before converting. Qlik tolerates them; an Import model pays for each column.

## Phase 3: Target architecture (week 2)
- **The QVD layer:** QVDs can't be read by Power BI. Either point tables at the
  QVDs' original sources, or land the staging layer as Dataflows Gen2 or a Fabric
  Lakehouse (Delta/Parquet) and read that.
- One semantic model per subject area with thin reports on top, instead of one
  model per app when several apps load the same data.
- Import by default; incremental refresh for large facts; DirectQuery or Direct
  Lake where freshness or size demands it.

## Phase 4: Model (weeks 2-4)
- `qlik2pbi convert`. Work the model part of the worklist in this order:
  1. tables without a query (QVD, loops, calendars): write the Power Query or point
     at the new staging layer;
  2. synthetic keys: add composite keys or link tables;
  3. relationship assumptions: after the first refresh, confirm the one-side keys
     are unique, and switch to many-to-many where they aren't;
  4. inactive relationships from circular references: decide the path;
  5. build a proper date table (CALENDAR) and mark it.
- Set the `Lib ...` folder parameters, then credentials, then the gateway.

## Phase 5: Expressions and pages (weeks 3-6)
- MANUAL measures: rebuild inter-record functions as visual calculations; rewrite
  `P()`/`E()`/`+=` set analysis explicitly.
- ASSUMED measures: check each stated assumption (`{1}` in charts, TOTAL, Aggr,
  `$(=...)` evaluation).
- Pages: replace extension objects, recreate alternate-state comparisons, bookmarks,
  conditional formatting and themes.
- NPrinting reports become paginated reports on the same model.

## Phase 6: Validate
- Structural checks run with every conversion (exit code 2 on converter defects).
- Data parity: run each `validation/parity-queries/*.dax` and compare it with the same
  table exported from Qlik (same selections); agree a tolerance.
- Security: replace DOMAIN\user with Entra UPNs; test every role with *View as role*
  against Qlik section access for sample users.
- Performance: Performance Analyzer (< 3 s per visual); refresh within its window.

## Phase 7: Cut over
- Publish; set the refresh schedules (from reload tasks), RLS membership,
  workspaces and apps (from streams/spaces), and subscriptions (from NPrinting).
- Run in parallel for one or two cycles, freeze the Qlik apps, redirect users,
  then decommission.
- Train users by role. Qlik users miss green/white/grey selections: show them
  slicers, cross-filtering and bookmarks early.
