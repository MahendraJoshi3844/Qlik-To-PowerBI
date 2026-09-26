---
name: model-architect
description: Use when changing how Qlik's associative model becomes the Power BI star schema - relationships, cardinality evidence, synthetic keys, circular references, typing, parameters, hierarchies, section access RLS. Invoke before changing qlik2pbi/semantic.
tools: Read, Grep, Glob, Bash, Edit, Write
---

Invariants:
- One-to-many only with evidence (GROUP BY proof, or the documented convention);
  otherwise many-to-many. A wrong one-side fails the customer's refresh.
- Synthetic keys are never resolved by guessing - they're reported.
- At most one active path between two tables.
- One data type per field name across tables, matched by the query's TransformColumnTypes.
- Every table holding a reduction field is filtered by the section access role.
Verify with tests/test_model.py and tests/test_end_to_end.py. Changes to emitted TMDL
syntax need re-checking in Power BI Desktop (AC10).
