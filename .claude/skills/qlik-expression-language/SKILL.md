---
name: qlik-expression-language
description: Reference for Qlik load-script and chart-expression syntax (quoting, set analysis, qualifiers, dollar-sign expansion, script prefixes) and the Power Query / DAX patterns qlik2pbi uses. Use when reading customer Qlik apps or extending the translators.
---

# Quoting
`'text'` is a string. `"Field"` and `[Field]` are field names. `` `Field` `` also works in scripts.

# Chart expressions
`Sum({<Year={2024}, Region={'North'}>} DISTINCT TOTAL <Region> Sales)`
- Set expression: `{identifier <modifiers>}`. Identifiers: `$` (current), `1` (all data), a
  bookmark id or an alternate state.
- Modifiers: `Field={...}` replaces the selection; `Field=` clears it; `+=` / `-=` / `*=` / `/=`
  combine with the current selection.
- Elements: numbers, `'strings'`, `"search strings"` (`"*"`, `"A*"`, `">=10<20"`,
  `"=expr"`), `$(=expr)`, `P(Field)` / `E(Field)`.
- Qualifiers: `DISTINCT`, `NODISTINCT`, `TOTAL [<fields>]`, `ALL` (= `{1} TOTAL`).
- `Aggr(inner, dim1, dim2)` builds a virtual table; the outer aggregation iterates it.
- Inter-record: `Above/Below/Before/After/RowNo/RangeSum(Above(...))`, which become visual calculations.

# Dollar-sign expansion
`$(vName)` is replaced by the variable's text before parsing. `$(=expr)` is evaluated once in
the selection state. `$(v(1))` (parameters) is not supported.

# Load script
`Label: [prefixes] LOAD [DISTINCT] fields FROM src (format) | RESIDENT T | INLINE [...] |
AUTOGENERATE n [WHERE ...] [GROUP BY ...];`, or `SQL SELECT ...;` after a CONNECT.
- A LOAD with no source is a **preceding load** on the next statement.
- Prefixes: MAPPING, CONCATENATE [(T)], NOCONCATENATE, [LEFT|RIGHT|INNER|OUTER] JOIN [(T)],
  [LEFT|RIGHT|INNER] KEEP [(T)], CROSSTABLE(attr, data, n), GENERIC, HIERARCHY, INTERVALMATCH, FIRST n.
- Tables sharing **field names** associate; two or more shared fields form a synthetic key.

# Where it lives
Script: `qlik2pbi/script/*`, `qlik2pbi/expr/to_m.py`. Expressions: `qlik2pbi/expr/parser.py`,
`qlik2pbi/expr/to_dax.py` (the pattern table is in its docstring).
