"""ReportPlan -> PBIR report definition files.

Schema versions are the ones Power BI Desktop is known to open unconditionally
(established on the sibling Tableau project and pinned in tests):
definition 4.0, report 1.3.0, page 1.0.0, visualContainer 1.4.0. Do not raise
them without opening an output in Desktop first.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qlik2pbi.report.layout import FieldRef_ as FieldRef, ReportPlan, VisualPlan

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
DEFINITION_VERSION = "4.0"
REPORT_VERSION = "1.3.0"
PAGE_VERSION = "1.0.0"
VISUAL_VERSION = "1.4.0"


def short_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def projection(ref: FieldRef) -> dict:
    kind = "Measure" if ref.is_measure else "Column"
    return {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": ref.table}}, "Property": ref.column}},
        "queryRef": f"{ref.table}.{ref.column}",
        "nativeQueryRef": ref.column,
    }


def visual_json(v: VisualPlan, vid: str, z: int) -> dict:
    return {
        "$schema": f"{SCHEMA}/visualContainer/{VISUAL_VERSION}/schema.json",
        "name": vid,
        "position": {"x": v.x, "y": v.y, "z": z, "width": v.width, "height": v.height, "tabOrder": z},
        "visual": {
            "visualType": v.visual_type,
            "query": {
                "queryState": {
                    well: {"projections": [projection(r) for r in refs]}
                    for well, refs in v.wells.items() if refs
                }
            },
            "drillFilterOtherVisuals": True,
        },
    }


def write_report(plan: ReportPlan, report_dir: Path, project: str) -> None:
    definition = report_dir / "definition"
    write_json(definition / "version.json",
               {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": DEFINITION_VERSION})
    write_json(definition / "report.json", {"$schema": f"{SCHEMA}/report/{REPORT_VERSION}/schema.json"})

    page_ids: list[str] = []
    pages = plan.pages or []
    for page in pages:
        pid = short_id(f"page:{project}:{page.name}")
        page_ids.append(pid)
        pdir = definition / "pages" / pid
        write_json(pdir / "page.json", {
            "$schema": f"{SCHEMA}/page/{PAGE_VERSION}/schema.json",
            "name": pid,
            "displayName": page.name,
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
        })
        for z, v in enumerate(page.visuals):
            if not any(v.wells.values()):
                continue  # never write a visual with an empty query
            vid = short_id(f"visual:{project}:{page.name}:{z}:{v.name}")
            write_json(pdir / "visuals" / vid / "visual.json", visual_json(v, vid, z))
    if not page_ids:
        pid = short_id(f"page:{project}:__empty__")
        page_ids.append(pid)
        write_json(definition / "pages" / pid / "page.json", {
            "$schema": f"{SCHEMA}/page/{PAGE_VERSION}/schema.json",
            "name": pid, "displayName": "Page 1", "displayOption": "FitToPage", "height": 720, "width": 1280,
        })
    write_json(definition / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": page_ids,
        "activePageName": page_ids[0],
    })
