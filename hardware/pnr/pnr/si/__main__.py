"""CLI: ``python -m pnr.si {report,design-check,fetch}`` (pnr runtime).

    report <board.kicad_pcb> --rules R [--graph placed.json --annotation-source A.ato ...]
           --out DIR [--report-name si.json] [--series-ohm OHM] [--workers N] [--timeout S] [--save-decks]
    design-check --graph placed.json --annotation-source A.ato [--rules R] [--out DIR] [--series-ohm OHM]
    fetch                      # fetch/verify every pinned vendor file into the cache

Prints the report summary and the evaluation side fields as JSON. The CLI does not
consult ``PNR_SI``: running it is the opt-in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _components(graph):
    if not graph:
        return None
    from pnr.graph import BoardGraph

    return BoardGraph.from_json(Path(graph).read_text()).components


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m pnr.si",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("board", type=Path)
    r.add_argument("--rules", type=Path, required=True)
    r.add_argument("--graph", type=Path)
    r.add_argument("--annotation-source", action="append", default=[], type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument(
        "--report-name",
        default="si.json",
        help="report file name inside --out (flows: si-report.json)",
    )
    r.add_argument("--series-ohm", type=float)
    r.add_argument("--workers", type=int)
    r.add_argument("--timeout", type=float)
    r.add_argument("--save-decks", action="store_true")
    d = sub.add_parser("design-check")
    d.add_argument("--graph", type=Path, required=True)
    d.add_argument("--annotation-source", action="append", default=[], type=Path, required=True)
    d.add_argument("--rules", type=Path)
    d.add_argument("--out", type=Path)
    d.add_argument("--series-ohm", type=float)
    d.add_argument("--workers", type=int)
    sub.add_parser("fetch")
    a = ap.parse_args(argv)
    from pnr.si import annotations as ann
    from pnr.si import models
    from pnr.si import report as rep

    if a.cmd == "fetch":
        lib = models.Library()
        out = {}
        for ident in lib.ids("driver"):
            for v in lib.get("driver", ident)["vendor_files"]:
                out[v["url"]] = str(models.fetch_vendor(v))
        print(json.dumps(out, indent=1))
        return 0
    if a.cmd == "report":
        rules = json.loads(a.rules.read_text())
        intents = rules.get("si_intents")
        if a.annotation_source:
            lib = models.Library()
            reqs, waivers = ann.parse(a.annotation_source)
            intents = ann.resolve(reqs, waivers, _components(a.graph) or [], lib)
        if intents is None:
            ap.error("rules carry no si_intents: pass --graph and --annotation-source")
        report = rep.post_route_report(
            a.board,
            rules,
            out_dir=a.out,
            intents=intents,
            series_ohm=a.series_ohm,
            workers=a.workers,
            timeout=a.timeout,
            save_decks=a.save_decks,
            report_name=a.report_name,
        )
    else:
        lib = models.Library()
        reqs, waivers = ann.parse(a.annotation_source)
        intents = ann.resolve(reqs, waivers, _components(a.graph), lib)
        rules = json.loads(a.rules.read_text()) if a.rules else None
        try:
            report = rep.design_check(
                intents,
                lib,
                rules=rules,
                out_dir=a.out,
                series_ohm=a.series_ohm,
                workers=a.workers,
                raise_on_fail=False,
            )
        except rep.SIDesignError as error:  # pragma: no cover - raise_on_fail=False
            report = error.report
    print(
        json.dumps(dict(summary=report["summary"], side_fields=rep.side_fields(report)), indent=1)
    )
    return 0 if not report["summary"].get("errors") else 2


if __name__ == "__main__":
    sys.exit(main())
