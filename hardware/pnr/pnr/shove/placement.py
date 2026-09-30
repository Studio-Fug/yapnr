"""Hard placement legality of make-room part nudges (G0/G3, PNR_SHOVE=1).

KiCad side (:func:`board_facts`, pcbnew): which footprints moved between two
boards, by how much from the flow's ORIGIN pose, whether any moved part is
locked, rotated, flipped or source-owned (a plane-access power array or copper
keepout owner, which pnr.incremental_place also refuses to move), plus each
board's :class:`pnr.graph.BoardGraph` JSON.

PnR side (:func:`new_violations`, ``python -m pnr.shove.placement``; needs yaml,
which KiCad's Python lacks): ``pnr.place.metrics.hard_violations`` of both graphs
under the loop's constraints.yaml (courtyard overlaps, outline, fixed poses and
rotations, sides, keepouts, HARD group radii and rows). A nudge may add no
violation. Mounting holes are dropped as in ``native_loop.placements``.
"""

import argparse
import json
import math
from pathlib import Path

TOTAL_CAP_MM = 0.5  # a part's cumulative make-room displacement from its origin pose


def protected_refs(rules):
    """Source-owned parts that may never move independently (as incremental_place)."""
    refs = {
        i["ref"] for i in rules.get("plane_access_intents", []) if i.get("kind") == "power_array"
    }
    refs |= {i["ref"] for i in rules.get("copper_keepouts", []) if i.get("ref")}
    return refs


def poses(board):
    """``{ref: (x, y, rotation_deg, layer)}`` native mm of every footprint."""
    return {
        f.GetReference(): (
            f.GetPosition().x / 1e6,
            f.GetPosition().y / 1e6,
            round(f.GetOrientationDegrees(), 6),
            f.GetLayer(),
        )
        for f in board.GetFootprints()
    }


def origin_offsets(board, origin):
    """``{ref: (dx, dy)}`` current minus origin pose (native mm) for ``origin`` =
    ``{ref: (x, y, ...)}``; parts absent from ``origin`` count as unmoved."""
    out = {}
    for ref, (x, y, *_rest) in poses(board).items():
        o = origin.get(ref)
        out[ref] = (x - o[0], y - o[1]) if o is not None else (0.0, 0.0)
    return out


def board_facts(before, after, rules, origin=None, cap=TOTAL_CAP_MM):
    """Footprint changes of ``after`` against ``before`` (live pcbnew boards):
    ``{moved: [...], problems: [...], graphs: (before_json, after_json) or None}``."""
    from pnr.ingest import build_graph

    a, b = poses(before), poses(after)
    origin = origin or a
    locked = {f.GetReference() for f in after.GetFootprints() if f.IsLocked()}
    owned = protected_refs(rules)
    moved, problems = [], []
    for ref, (x, y, rot, layer) in sorted(b.items()):
        old = a.get(ref)
        if old is None:
            problems.append(dict(ref=ref, problem="new_footprint"))
            continue
        step = math.dist((x, y), old[:2])
        if step <= 1e-6 and rot == old[2] and layer == old[3]:
            continue
        o = origin.get(ref, old)
        total = math.dist((x, y), o[:2])
        moved.append(dict(ref=ref, step_mm=round(step, 5), total_mm=round(total, 5)))
        if rot != old[2] or layer != old[3]:
            problems.append(dict(ref=ref, problem="rotated_or_flipped"))
        if ref in locked:
            problems.append(dict(ref=ref, problem="locked"))
        if ref in owned:
            problems.append(dict(ref=ref, problem="source_owned_array_or_keepout"))
        if total > cap + 1e-6:
            problems.append(
                dict(ref=ref, problem="cumulative_cap", total_mm=round(total, 5), cap_mm=cap)
            )
    graphs = None
    if moved:
        graphs = (
            json.loads(build_graph(before).to_json()),
            json.loads(build_graph(after).to_json()),
        )
    return dict(moved=moved, problems=problems, graphs=graphs)


def _key(value):
    return json.dumps(value, sort_keys=True)


def violations(graph_json, doc):
    from pnr.graph import BoardGraph
    from pnr.constraints import compile_constraints
    from pnr.place.metrics import hard_violations

    g = BoardGraph.from_json(json.dumps(graph_json))
    cc = compile_constraints(
        doc,
        g.refs,
        {c.address: c.ref for c in g.components},
        {f"{c.address}:{p.name}": p.net for c in g.components for p in c.pads},
    )
    holes = {h["name"] for h in cc.mounting_holes}
    g.components = [c for c in g.components if c.ref not in holes]
    return {
        kind: sorted((json.loads(json.dumps(v)) for v in values), key=_key)
        for kind, values in hard_violations(g, cc).items()
    }


def new_violations(before_json, after_json, constraints_path):
    """Hard violations of ``after`` that ``before`` does not have, by kind."""
    import yaml

    doc = yaml.safe_load(Path(constraints_path).read_text())
    a, b = violations(before_json, doc), violations(after_json, doc)
    out = {}
    for kind, values in b.items():
        old = {_key(v) for v in a.get(kind, [])}
        new = [v for v in values if _key(v) not in old]
        if new:
            out[kind] = new
    return dict(
        new=out, before={k: v for k, v in a.items() if v}, after={k: v for k, v in b.items() if v}
    )


def check(
    before, after, rules, constraints, python, work, origin=None, cap=TOTAL_CAP_MM, timeout=120
):
    """G0/G3 for live boards (KiCad side): ``{accepted, moved, problems, new_violations}``.

    No moved footprint: accepted. Otherwise the part-level problems must be empty
    and ``python -m pnr.shove.placement`` (the PnR runtime, ``python``) must find no
    new hard violation under ``constraints``; without either the nudge is not
    verifiable and is rejected."""
    import os
    import subprocess
    from pnr.proc import run

    facts = board_facts(before, after, rules, origin, cap)
    out = dict(moved=facts["moved"], problems=list(facts["problems"]), new_violations={})
    if not facts["moved"]:
        out["accepted"] = True
        return out
    if not constraints or not python:
        out["problems"].append(
            dict(problem="unverifiable_nudge", detail="no --constraints/--placement-python")
        )
        out["accepted"] = False
        return out
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "placement-before.json").write_text(json.dumps(facts["graphs"][0]))
    (work / "placement-after.json").write_text(json.dumps(facts["graphs"][1]))
    report = work / "placement.json"
    with (work / "placement.log").open("w") as stream:
        code = run(
            [
                str(python),
                "-m",
                "pnr.shove.placement",
                str(work / "placement-before.json"),
                str(work / "placement-after.json"),
                "--constraints",
                str(constraints),
                "--out",
                str(report),
            ],
            timeout=timeout,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
    if code or not report.exists():
        out["problems"].append(dict(problem="placement_check_failed", returncode=code))
        out["accepted"] = False
        return out
    verdict = json.loads(report.read_text())
    out["new_violations"] = verdict["new"]
    out["accepted"] = not out["problems"] and not verdict["new"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--constraints", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    result = new_violations(
        json.loads(a.before.read_text()), json.loads(a.after.read_text()), a.constraints
    )
    a.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(dict(new=result["new"])))


if __name__ == "__main__":
    main()
