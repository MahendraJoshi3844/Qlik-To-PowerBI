# Input formats

qlik2pbi reads app **metadata and script text only**. It never reads data, QVDs or
the app's in-memory model.

## 1. `qlik app unbuild` folder (primary, Qlik Sense)

```
qlik app unbuild --app <app-id> --dir sales_app/
```

| File | Used for |
|---|---|
| `script.qvs` | The load script: the data model |
| `connections.yml` | Data connections: `folder` paths for `lib://`, connection strings for CONNECT |
| `variables.json` | `[{qName, qDefinition}]`, expanded as `$(name)` |
| `dimensions.json` | Master dimensions: `qDim.qFieldDefs`, `qGrouping` (N / H drill-down / C cyclic) |
| `measures.json` | Master measures: `qMeasure.qDef`, `qLabel`, `qNumFormat.qFmt` |
| `objects/*.json` | Sheets (`qType: sheet`, `cells[{name,col,row,colspan,rowspan}]`) and visualizations (`qHyperCubeDef.qDimensions/qMeasures`, `qListObjectDef` for list boxes, `qChildList` for filter panes, `qStateName`) |
| `app-properties.json` | `qTitle`, `qStateNames` (alternate states) |
| `bookmarks.json` | Optional: bookmark names |

The folder may be zipped. Unknown files are listed under *Input not recognised*
by `qlik2pbi inspect`.

## 2. Single JSON

One object whose keys are the parts above: `script`, `connections`, `variables`,
`dimensions`, `measures`, `objects` (a list), `app-properties`, `bookmarks`. This is
the shape of an Engine API export assembled by a script.

## 3. Load script only (`.qvs`)

Converts the data model (tables, relationships, types); no pages or measures.

## 4. QlikView `-prj` folder

Only `LoadScript.txt` is read today. The XML object files are listed as not
recognised; extending the reader to them is on the task list.

## 5. Not read directly

`.qvf` and `.qvw` are proprietary binary containers. The loader refuses them and
names the export route above. (Reverse-engineering the binary format is
deliberately out of scope: it is fragile across Qlik releases and not needed when
qlik-cli can export the same content.)

`samples/sales_app/` is a complete synthetic unbuild folder; regenerate it with
`python samples/generate_sales_app.py samples/sales_app`.
