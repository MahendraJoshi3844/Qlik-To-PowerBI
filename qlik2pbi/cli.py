"""Command line: qlik2pbi inspect | assess | convert."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qlik2pbi import __version__
from qlik2pbi.findings import Fidelity


def _inspect(args) -> int:
    from qlik2pbi.app.loader import load_app
    from qlik2pbi.findings import FindingLog
    from qlik2pbi.script.interpret import interpret

    app = load_app(Path(args.input))
    print(f"App: {app.name}  (source: {app.source_kind})")
    for key, n in app.counts().items():
        if n:
            print(f"  {key:<18} {n}")
    sm = interpret(app, FindingLog())
    print(f"  script statements  {sm.statement_count}")
    for t in sm.loaded():
        state = "query" if t.m else "NO QUERY: " + "; ".join(t.manual)[:100]
        print(f"    table {t.name:<22} {len(t.fields):>3} fields  {state}")
    if sm.features:
        print("  script features    " + ", ".join(f"{k} {v}" for k, v in sorted(sm.features.items())))
    if app.unrecognized:
        print("\nNot recognised (share these so the reader can be extended):")
        for u in app.unrecognized:
            print(f"  - {u}")
    return 0


def _summary(run) -> None:
    print(f"App: {run.app.name}")
    a = run.assessment
    if a:
        print(f"  Readiness (assessed): {a.readiness_pct}%   effort: {a.effort['manual_rebuild_hours']} h manual "
              f"-> {a.effort['with_accelerator_hours']} h with qlik2pbi")
    if run.model:
        data = [t for t in run.model.tables if t.kind == "data"]
        queried = sum(1 for t in data if t.has_query)
        ms = run.model.all_measures()
        ok = sum(1 for m in ms if m.fidelity in (Fidelity.EXACT, Fidelity.ASSUMED))
        print(f"  Tables with a query: {queried}/{len(data)}   relationships: {len(run.model.relationships)}   "
              f"measures translated: {ok}/{len(ms) + len(run.withheld)}   roles: {len(run.model.roles)}")
    if run.plan:
        print(f"  Pages: {len(run.plan.pages)}   visuals: {sum(len(p.visuals) for p in run.plan.pages)}")
    print("  Findings: " + ", ".join(f"{f.value} {run.log.count(f)}" for f in Fidelity))
    if run.defects:
        print(f"  !! {run.defects} converter defect(s) found by validation - see the report.")
    if run.pbip:
        print(f"  Power BI project: {run.pbip}")
    print(f"  Report: {run.report_html}")
    print("  Stage timings (s): " + ", ".join(f"{k} {v}" for k, v in run.timings.items()))


def _convert(args, assess_only: bool) -> int:
    from qlik2pbi.pipeline import Options, run

    r = run(args.input, args.out, Options(project_name=args.name or "", assess_only=assess_only,
                                          placeholders=not getattr(args, "no_placeholders", False)))
    _summary(r)
    return 2 if r.defects else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qlik2pbi", description="Qlik Sense / QlikView to Power BI migration accelerator")
    p.add_argument("--version", action="version", version=f"qlik2pbi {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("inspect", help="Show what an input contains, how the script reads, and what was not recognised")
    s.add_argument("input")
    for name, help_ in (("assess", "Inventory, complexity, effort and rationalization only"),
                        ("convert", "Full migration to a Power BI project (PBIP) plus report")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("input", help="qlik app unbuild folder, .zip, .json or .qvs")
        s.add_argument("--out", required=True, help="output folder")
        s.add_argument("--name", help="Power BI project name (default: the app title)")
        s.add_argument("--no-placeholders", action="store_true",
                       help="emit nothing for untranslatable expressions instead of BLANK() placeholder measures")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from qlik2pbi.app.loader import UnsupportedInput

    try:
        if args.command == "inspect":
            return _inspect(args)
        return _convert(args, assess_only=args.command == "assess")
    except (FileNotFoundError, UnsupportedInput, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
