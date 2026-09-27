Convert a Qlik app end to end and summarise it: $ARGUMENTS

1. `qlik2pbi inspect "$ARGUMENTS"`. If anything is listed as not recognised, report it first.
2. `qlik2pbi convert "$ARGUMENTS" --out out/app`
3. From `out/app/migration_report.json`, summarise: tables with/without a query and why,
   relationships (assumed and inactive), expressions translated, the top 10 worklist items,
   the unused fields to drop, and any converter defects (exit code 2).
4. Remind the user that AC10 (open and refresh in Power BI Desktop) needs a human.
Customer apps stay in `customer/`; never `git add` them.
