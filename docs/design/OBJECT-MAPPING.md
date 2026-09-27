# Mapping matrix: Qlik → Power BI

**E** = exact · **A** = assumed (stated) · **M** = manual (worklist) · **I** = info / checklist

## Load script → Power Query

| Qlik | Power Query M | Tier |
|---|---|---|
| `LIB CONNECT` + `SQL SELECT` (SQL Server, Postgres, Oracle, MySQL, Snowflake, Teradata) | `Value.NativeQuery(<connector>, sql, null, [EnableFolding=true])` | E |
| Unknown connector | `Odbc.DataSource` | A |
| `FROM lib://Folder/x.csv (txt, ...)` | `Csv.Document(File.Contents(#"Lib Folder" & "x.csv"), ...)` + headers | E |
| `FROM ... (ooxml, table is S)` | `Excel.Workbook(...){[Item=S,Kind="Sheet"]}[Data]` | E |
| `FROM ... (qvd)` | – (the QVD source or a Lakehouse instead) | M |
| `LOAD * INLINE [...]` | `#table(type table [...], {...})` | E |
| `RESIDENT T` | T's M as of that statement | E |
| Preceding load | Steps applied on top of the loaded result | E |
| `WHERE` (translatable) | `Table.SelectRows` | E |
| Field expressions | `Table.AddColumn` (see the functions below) | E / A |
| `DISTINCT`, `FIRST n` | `Table.Distinct`, `Table.FirstN` | E |
| `GROUP BY` + Sum/Min/Max/Avg/Count | `Table.Group` | E |
| `CROSSTABLE(a, d, n)` | `Table.UnpivotOtherColumns` | E |
| `MAPPING LOAD` + `ApplyMap` | Buffered first-match lookup | E |
| `CONCATENATE`, automatic concatenation | `Table.Combine` | E / A |
| `LEFT/INNER JOIN` | `Table.NestedJoin` (keys as text) | E |
| `RIGHT/OUTER JOIN` | NestedJoin + key merge | E |
| `LEFT/RIGHT/INNER KEEP` | Semi-join | E |
| `QUALIFY`, `RENAME FIELD/TABLE`, `DROP FIELD/TABLE` | Rename / remove / not loaded | E |
| `SET`/`LET` + `$(v)` | Textual expansion; LET only when constant | E / M |
| `AUTOGENERATE` calendars | DAX `CALENDAR` table | M |
| `FOR`/`DO` loops | `Folder.Files` + a function | M |
| `IF` blocks | Assumed to run | A |
| `INTERVALMATCH`, `GENERIC`, `HIERARCHY`, `BINARY`, `$(Include)` | – | M |
| `STORE ... (qvd)` | Dataflows / Lakehouse (checklist) | I |
| Script functions: Year/Month/Day, Upper/Lower/Trim, Left/Right/Mid, Len, Replace, If, Alt, Round, Floor/Ceil, MakeDate, MonthStart, Match, Pick, `&`, `/` | `Date.*`, `Text.*`, `if`, `??`, `Number.Round(...AwayFromZero)` ...; `&` → `Text.Combine` (null-safe); `/` null on zero | E |
| Capitalize, Num/Date formatting | `Text.Proper`, value kept | A |

## Associative model → star schema

| Qlik | Power BI | Tier |
|---|---|---|
| Two tables sharing one field | Relationship on that field | – |
| Key proven unique (GROUP BY) | Many-to-one | E |
| Key assumed unique (first field, less fact-like) | Many-to-one | A |
| No uniqueness evidence (incl. CROSSTABLE, concatenated) | Many-to-many | A |
| Synthetic key (≥2 shared fields) | – (composite key or link table) | M |
| Circular reference | Inactive relationship | A |
| Qlik typeless fields | Typed columns (from usage) + `TransformColumnTypes` | A |
| `lib://` folder | Query parameter (`expressions.tmdl`) | I |
| Drill-down group (one table) | Hierarchy | E |
| Drill-down group (several tables) | Place the fields in order on the axis | I |
| Cyclic group | Field parameter | M |

## Expressions → DAX

| Qlik | DAX | Tier |
|---|---|---|
| `Sum/Avg/Min/Max/Median/Stdev(F)` | `SUM/AVERAGE/...('T'[F])` | E |
| `Count(F)` / `Count(DISTINCT F)` | `COUNTA` / `DISTINCTCOUNTNOBLANK` | E |
| `Sum(DISTINCT F)` | `SUMX(DISTINCT(col), col)` | E |
| `Sum(a*b)` (same table / related one side) | `SUMX('T', ...)` / with `RELATED()` | E |
| Key field present in several tables | Aggregated from the most fact-like table | A |
| `Only`, `MaxString`, `MinString`, `Fractile` | `SELECTEDVALUE`, `MAX`, `MIN`, `PERCENTILE.INC` | E |
| `Concat` | `CONCATENATEX` | A (order) |
| `a / b` | `DIVIDE(a, b)` | E |
| `{<F={v}>}`, `F=`, `{"*"}`, numeric search, `*=`, `-=` | `CALCULATE` filters | E |
| Wildcard search | `LEFT/RIGHT/CONTAINSSTRING` | A |
| `$(=expr)` in a set | `VAR` = `CALCULATE(expr, ALLSELECTED())` | A |
| `{1}` | `REMOVEFILTERS()` (+ chart dimensions) | A |
| `TOTAL [<d>]` | `ALLSELECTED()` (+ `VALUES(d)`) | A |
| `Agg(Aggr(inner, d...))` | `AggX(VALUES(d) or SUMMARIZE(...), CALCULATE(inner))` | A |
| `Rank(x)` in a 1-dimension chart | `RANKX(ALLSELECTED(d), ...)` | A |
| `Column(n)` | The chart's n-th measure | E |
| `Num(x, fmt)` at top level | Value + `formatString` | E |
| Field outside an aggregation | `SELECTEDVALUE` | A |
| `[Master measure label]` | Measure reference | E |
| `+=`, `/=`, `P()`, `E()`, set operators, bookmarks/states in sets | – | M |
| `Above/Below/RowNo/RangeSum(Above())...` | Visual calculations | M |
| `GetSelectedCount`, `GetFieldSelections`... | `ISFILTERED/VALUES` by hand | M |
| Scalars: If, Alt, Round, Floor, Ceil, Fabs, Sqrt, Exp, Log, Pow, Mod, Div, Len, Upper, Lower, Trim, Left, Right, Mid, Index, Replace, Year, Month, Day, Quarter, WeekDay, Today, Now, MakeDate, MonthStart, YearStart, AddMonths, Match, Pick | DAX equivalents | E / A (Week, MonthEnd, Match case) |

## Front end

| Qlik | Power BI | Tier |
|---|---|---|
| Sheet | Page (grid scaled) | E |
| Bar (vertical/horizontal × grouped/stacked), line, area, pie, donut, combo, table, pivot, KPI, gauge, scatter, treemap | Matching visual | E |
| Waterfall, map, funnel extension | Matching visual | A |
| Filter pane / list box | Slicer per field | E |
| Master measure | Measure (folder "Master measures") | per expression |
| Chart expression | Measure (folder "Chart expressions\\<sheet>"), reusing masters | per expression |
| Calculated dimension | Calculated column | A |
| Box plot, distribution plot, histogram, Mekko, text & image, button, container, extensions | AppSource visual / by hand | M |
| Alternate states | Default state + worklist | M |
| Bookmarks, stories | Bookmarks / pages by hand | M |

## Security and operations

| Qlik | Power BI | Tier |
|---|---|---|
| Section access (USERID/NTNAME/USER.EMAIL + reductions) | Dynamic RLS role on every table holding the field | A |
| ADMIN rows | Unrestricted within the role | A |
| OMIT | Object-level security | M |
| Reload tasks | Scheduled refresh | I |
| Streams / spaces | Workspaces / app audiences | I |
| NPrinting | Paginated reports + subscriptions | I |
| QVD layer | Dataflows Gen2 / Fabric Lakehouse | I |
