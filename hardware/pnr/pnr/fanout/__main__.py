"""``python -m pnr.fanout``: plan declared fanouts, or verify them on a board.

``plan``    (any Python): a graph JSON (``pnr.ingest``/``placed.json``) and the routing
            rules (``rules.json`` with ``fanouts``) -> ``fanout-<name>.json`` per
            fanout in ``--out``, and a one-line summary each.
``verify``  (KiCad's Python): a board and its rules -> the plans, the board copy with
            the fanout copper, the native Oracle and KiCad DRC verdicts
            (``verify.json``); see :mod:`pnr.fanout.verify`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pnr.fanout", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="plan every declared fanout of a graph")
    p.add_argument("graph", type=Path)
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--fixed", type=Path, help="fixed copper (pnr.fixed_copper export)")
    p.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify", help="plan, add to a board copy and judge (KiCad Python)")
    v.add_argument("board", type=Path)
    v.add_argument("--rules", type=Path, required=True)
    v.add_argument("--fixed", type=Path)
    v.add_argument("--out", type=Path, required=True)
    v.add_argument("--kicad-cli", help="kicad-cli for the DRC (omit: Oracle only)")
    a = ap.parse_args(argv)
    rules = json.loads(a.rules.read_text())
    fixed = json.loads(a.fixed.read_text()) if a.fixed else None
    if not rules.get("fanouts"):
        ap.error("the rules declare no fanout")
    if a.command == "plan":
        from pnr.graph import BoardGraph

        from .planner import classify, plan

        graph = BoardGraph.from_json(a.graph.read_text())
        layers, drops, signals = classify(graph, rules)
        a.out.mkdir(parents=True, exist_ok=True)
        for spec in rules["fanouts"]:
            result = plan(
                graph,
                rules,
                spec,
                grid_layers=layers,
                plane_nets=drops,
                signal_nets=signals,
                fixed_copper=fixed,
            )
            (a.out / ("fanout-%s.json" % spec["name"])).write_text(json.dumps(result, indent=1))
            d = result["diagnostics"]
            print(
                "%s: %d/%d signals escaped, %d/%d drops, %d vias, %d tracks"
                % (
                    spec["name"],
                    d["signals_escaped"],
                    d["signals"],
                    d["drops_placed"],
                    d["drops"],
                    len(result["copper"]["vias"]),
                    len(result["copper"]["tracks"]),
                )
            )
        return 0
    from .verify import verify

    report = verify(a.board, rules, a.out, kicad_cli=a.kicad_cli, fixed=fixed)
    summary = dict(
        plans=report["plans"],
        oracle_rejected=len(report["oracle"]["rejected"]),
        drc=report.get("drc"),
    )
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
