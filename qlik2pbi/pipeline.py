"""The migration pipeline - the only module that knows the stage order.

    read -> script -> assess -> model -> translate -> layout -> finalize -> emit -> validate -> report

* read      : unbuild folder / zip / json / .qvs -> QlikApp        (app/)
* script    : run the load script symbolically -> tables + M       (script/)
* assess    : complexity, effort, rationalization                  (assess/)
* model     : associative model -> star schema, types, parameters  (semantic/planner)
* translate : master measures -> DAX, section access -> RLS        (semantic/measures, expr/)
* layout    : sheets/objects -> pages/visuals, chart expressions   (report/)
* finalize  : dependency propagation, placeholders, pruning        (semantic/measures)
* emit      : PBIP (TMDL + PBIR)                                   (emit/)
* validate  : structural checks + data-parity DAX queries          (validate/)
* report    : migration_report.html / .json                        (output/)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from qlik2pbi.app.loader import load_app
from qlik2pbi.app.model import QlikApp
from qlik2pbi.assess.inventory import Assessment, assess
from qlik2pbi.assess.rationalize import Rationalization, rationalize
from qlik2pbi.emit.pbip import write_pbip
from qlik2pbi.expr.to_dax import ToDax
from qlik2pbi.findings import Fidelity, FindingLog
from qlik2pbi.output.report import build_json, write_reports
from qlik2pbi.report.layout import ReportPlan, plan_report
from qlik2pbi.script.interpret import ScriptModel, interpret
from qlik2pbi.semantic.measures import (
    finalize_measures,
    measure_names,
    translate_master_measures,
    translate_section_access,
    translator,
)
from qlik2pbi.semantic.model import SemanticModel
from qlik2pbi.semantic.planner import plan_model
from qlik2pbi.validate.checks import parity_queries, structural_checks


@dataclass
class Options:
    project_name: str = ""
    assess_only: bool = False
    #: Keep untranslatable expressions as BLANK() placeholder measures.
    placeholders: bool = True


@dataclass
class Run:
    input: Path
    out_dir: Path
    options: Options
    log: FindingLog = field(default_factory=FindingLog)
    app: QlikApp | None = None
    script: ScriptModel | None = None
    assessment: Assessment | None = None
    rationalization: Rationalization | None = None
    model: SemanticModel | None = None
    plan: ReportPlan | None = None
    names: dict[str, str] = field(default_factory=dict)
    translator: ToDax | None = None
    withheld: set[str] = field(default_factory=set)
    pbip: Path | None = None
    report_json: Path | None = None
    report_html: Path | None = None
    parity_files: list[Path] = field(default_factory=list)
    defects: int = 0
    timings: dict[str, float] = field(default_factory=dict)


def _read(run: Run) -> None:
    run.app = load_app(run.input)
    if run.options.project_name:
        run.app.name = run.options.project_name


def _script(run: Run) -> None:
    run.script = interpret(run.app, run.log)


def _assess(run: Run) -> None:
    run.assessment = assess(run.app, run.script)
    run.rationalization = rationalize(run.app, run.script)


def _model(run: Run) -> None:
    run.model = plan_model(run.app, run.script, run.log)


def _translate(run: Run) -> None:
    run.names = measure_names(run.app, run.model, run.log)
    run.translator = translator(run.app, run.script, run.model, run.names)
    translate_master_measures(run.app, run.model, run.translator, run.names, run.log)
    translate_section_access(run.model, run.log)


def _layout(run: Run) -> None:
    run.plan = plan_report(run.app, run.model, run.translator, run.names, run.log)


def _finalize(run: Run) -> None:
    run.withheld = finalize_measures(run.model, run.log, run.options.placeholders)
    if not run.withheld:
        return
    # A visual may not point at a measure that was not written.
    for page in run.plan.pages:
        kept = []
        for v in page.visuals:
            for well in list(v.wells):
                gone = [f for f in v.wells[well] if f.is_measure and f.column in run.withheld]
                if gone:
                    v.wells[well] = [f for f in v.wells[well] if f not in gone]
                    run.log.add("layout", "field", f"{page.name} / {v.name}", Fidelity.MANUAL,
                                "Left out of the visual because it was not translated: "
                                + ", ".join(f.column for f in gone) + ".",
                                action="Add it back once the measure exists.")
                if not v.wells[well]:
                    del v.wells[well]
            if v.wells:
                kept.append(v)
        page.visuals = kept


def _emit(run: Run) -> None:
    run.pbip = write_pbip(run.model, run.plan, run.out_dir, run.app.name)


def _validate(run: Run) -> None:
    run.defects = structural_checks(run.model, run.plan, run.log)
    run.parity_files = parity_queries(run.plan, run.out_dir)


def _report(run: Run) -> None:
    data = build_json(run.app, run.script, run.assessment, run.rationalization, run.log, run.model, run.plan)
    run.report_json, run.report_html = write_reports(run.out_dir, data)


CONVERT = [("read", _read), ("script", _script), ("assess", _assess), ("model", _model), ("translate", _translate),
           ("layout", _layout), ("finalize", _finalize), ("emit", _emit), ("validate", _validate), ("report", _report)]
ASSESS_ONLY = [("read", _read), ("script", _script), ("assess", _assess), ("report", _report)]


def run(input_path: str | Path, out_dir: str | Path, options: Options | None = None) -> Run:
    r = Run(input=Path(input_path), out_dir=Path(out_dir), options=options or Options())
    r.out_dir.mkdir(parents=True, exist_ok=True)
    for name, stage in (ASSESS_ONLY if r.options.assess_only else CONVERT):
        started = time.perf_counter()
        stage(r)
        r.timings[name] = round(time.perf_counter() - started, 3)
    return r
