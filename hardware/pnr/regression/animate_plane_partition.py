"""Renders the plane-partition docs example: the radar60 U2 buck PMIC's outer pour (stage 3c
wave-4 library candidate ``34c7ed35931c``, ``docs/plane-partition.md``).

    bazel run //hardware/pnr:plane_partition_animation -- --out docs/images/plane-partition

Loads ``regression/fixtures/pmic_pour_34c7ed35931c.json`` (the candidate's real pad and
courtyard geometry, extracted once under KiCad's Python by
``regression/extract_pmic_fixture.py`` -- see that file's docstring and the fixture's own
``meta.note``) and re-runs ``pnr.plane_partition.partition`` on it with a
:class:`~pnr.plane_partition.PlaneTrace`, needing neither KiCad nor torch here. Writes
``plane-partition.webp`` and one ``plane-partition-<stage>.png`` still per stage, plus
``plane-partition-report.json`` (the partition's own report, for a reader comparing the
stills against the numbers in the text).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def load_fixture(path):
    from pnr.plane_partition import Terminal

    with open(path) as f:
        data = json.load(f)
    terms = {
        net: [
            Terminal(
                t["name"],
                t["kind"],
                tuple(t["at"]),
                t["radius"],
                tuple(t["size"]) if t.get("size") else None,
            )
            for t in rows
        ]
        for net, rows in data["terms"].items()
    }
    data = dict(data, terms=terms)
    data["blocked"] = [(tuple(c), r) for c, r in data["blocked"]]
    data["blocked_polygons"] = [
        ([[tuple(p) for p in ring] for ring in rings], frozenset(allowed))
        for rings, allowed in data["blocked_polygons"]
    ]
    return data


def run(fixture_path, out_dir, budget_mb=2.5, width=800):
    from pnr.animate.plane_partition import render_stills, render_webp
    from pnr.plane_partition import _CACHE, PlaneTrace, partition

    data = load_fixture(fixture_path)
    entry = data["entry"]
    trace = PlaneTrace(nets=list(entry["nets"]))
    _CACHE.clear()
    part = partition(
        entry,
        width=data["width"],
        height=data["height"],
        terminals=data["terms"],
        blocked=data["blocked"],
        blocked_polygons=data["blocked_polygons"],
        currents=data["currents"],
        budgets_mohm=data["budgets_mohm"],
        sources=data["sources"],
        copper_mm=data["copper_mm"],
        edge_mm=data["edge_mm"],
        via_drill_mm=data["via_drill_mm"],
        fill_min_mm=data["fill_min_mm"],
        foreign_lands=[[tuple(p) for p in ring] for ring in data["foreign_lands"]],
        corridor_mm=data["corridor_mm"],
        corridor_keep_mm=data["corridor_keep_mm"],
        bodies=[[tuple(p) for p in ring] for ring in data["bodies"]],
        trace=trace,
    )
    os.makedirs(out_dir, exist_ok=True)
    title = "radar60 U2 buck: the PMIC pour"
    settings = render_webp(
        trace,
        os.path.join(out_dir, "plane-partition.webp"),
        title=title,
        budget_bytes=int(budget_mb * 1024 * 1024),
        width=width,
    )
    stills = render_stills(trace, out_dir, prefix="plane-partition", title=title)
    with open(os.path.join(out_dir, "plane-partition-report.json"), "w") as f:
        json.dump(part.report, f, indent=1, sort_keys=True)
        f.write("\n")
    print("webp settings:", settings)
    print("stills:", stills)
    print("warnings:")
    for net, info in sorted(part.report["nets"].items()):
        for w in info.get("warnings", []):
            print(" -", w)
    unreached = {n: i["unreached"] for n, i in part.report["nets"].items() if i["unreached"]}
    if unreached:
        print("unreached terminals:", unreached)
    walled = part.report.get("walled_in")
    if walled:
        print("walled in:", walled)
    return part


def _repo_root():
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        return workspace
    # HERE is hardware/pnr/regression; three levels up is the repository root.
    return os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fixture",
        default=os.path.join(HERE, "fixtures", "pmic_pour_34c7ed35931c.json"),
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output directory for the webp/png/json (default <repo>/docs/images/plane-partition)",
    )
    ap.add_argument("--budget-mb", type=float, default=2.5)
    ap.add_argument("--width", type=int, default=800)
    args = ap.parse_args(argv)
    out = args.out or os.path.join(_repo_root(), "docs", "images", "plane-partition")
    run(args.fixture, out, budget_mb=args.budget_mb, width=args.width)


if __name__ == "__main__":
    main()
