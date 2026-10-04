"""Plane-pair ring-down models (D15, E1-PP): a square window of a zone-filled macro board
(board_export.py) for pp_sim.py, the stitched L1 pour over L2 and the L2-L3 bondply as built.

The window is centred on the variant's worst spot, the record's largest L2-L3 via-free circle
(`stitch metrics (D15)` -> l23_largest_gap_at), moved inside the macro's region if needed, or on
`--at X Y`. Everything of the board inside the window is kept: the L1 copper (GND fill and any
line, which runs into the PML at the window's edge), the vias, the L2 apertures (holes of the
In1.Cu fill). L3 is solid under the region. The sources and probes sit at the window's centre and
at two points 1 mm away on each pair (L1-L2 core and L2-L3 bond).

  python openems/board_pp.py BOARD.json RECORD.json OUT.json [--size 4.0] [--at X Y] [--plot PNG]

Needs shapely (and matplotlib for --plot).
"""

import argparse
import json
import os
import sys

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from board_ant import track_poly  # noqa: E402


def mp(poly):
    return unary_union([Polygon(r, h) for r, h in poly if len(r) >= 3]).buffer(0)


def parts(g):
    if g.is_empty:
        return []
    return list(g.geoms) if hasattr(g, "geoms") else [g]


def holefree(g):
    out = []
    todo = parts(g)
    while todo:
        q = todo.pop()
        if q.area < 1e-6:
            continue
        if not q.interiors:
            out.append(q)
            continue
        cx = q.interiors[0].centroid.x
        bx = q.bounds
        for half in (
            box(bx[0] - 1, bx[1] - 1, cx, bx[3] + 1),
            box(cx, bx[1] - 1, bx[2] + 1, bx[3] + 1),
        ):
            todo += [r for r in parts(q.intersection(half)) if isinstance(r, Polygon)]
    return [list(map(list, q.simplify(0.001).exterior.coords))[:-1] for q in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("record")
    ap.add_argument("out")
    ap.add_argument("--size", type=float, default=4.0)
    ap.add_argument("--at", type=float, nargs=2)
    ap.add_argument("--plot")
    a = ap.parse_args()
    d = json.load(open(a.board))
    rec = json.load(open(a.record))
    sm = next(c for c in rec["checks"] if c["check"].startswith("stitch metrics"))
    rg = rec["region_u1_mm"]
    h = a.size / 2
    cx, cy = a.at if a.at else sm["l23_largest_gap_at"]
    half_pkg = 5.2
    # inside the region, clear of the package
    cx = min(max(cx, rg["x"][0] + h), rg["x"][1] - h)
    cy = min(max(cy, rg["y"][0] + h), rg["y"][1] - h)
    if abs(cx) < half_pkg + h and abs(cy) < half_pkg + h:
        cy = max(cy, half_pkg + h)
    W = box(cx - h, cy - h, cx + h, cy + h)
    gnd = unary_union(
        [mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"]
    ).intersection(W)
    nets = {}
    for t in d["tracks"]:
        if t["layer"] == "F.Cu":
            nets.setdefault(t["net"], []).append(track_poly(t))
    for pd in d["pads"]:
        if pd["layer"] == "F.Cu" and pd["net"] != "GND":
            nets.setdefault(pd["net"], []).append(mp(pd["poly"]))
    nets = {k: unary_union(v).intersection(W) for k, v in nets.items()}
    nets = {k: v for k, v in nets.items() if not v.is_empty and v.area > 1e-5}
    # GND vias only (a PA via in the window is not a post of the plane pair)
    vias = [v for v in d["vias"] if W.contains(Point(v[0], v[1])) and (len(v) < 5 or v[4] == "GND")]
    l2 = unary_union([mp(f["poly"]) for f in d["fill"] if f["layer"] == "In1.Cu"])
    l2_holes = W.difference(l2)
    src = [cx, cy]
    probes = [[cx, cy], [cx + 1.0, cy], [cx, cy + 1.0]]
    model = dict(
        model="pp",
        board=a.board.split("/")[-1],
        variant=rec.get("board_frame", {}).get("options", {}).get("d15"),
        window=[round(v, 5) for v in W.bounds],
        gnd=holefree(gnd),
        nets={k: holefree(v) for k, v in nets.items()},
        vias=[[round(v[0], 5), round(v[1], 5), v[3]] for v in vias],
        l2_holes=holefree(l2_holes),
        source=src,
        probes=probes,
        gap_radius_mm=sm["l23_largest_gap_radius_mm"],
        l1_dmax_interior_mm=sm["l1_dmax_interior_mm"],
    )
    json.dump(model, open(a.out, "w"))
    print(
        a.out,
        "window",
        model["window"],
        "vias",
        len(vias),
        "gnd polys",
        len(model["gnd"]),
        "nets",
        sorted(nets),
        "l2 holes",
        len(model["l2_holes"]),
    )
    if a.plot:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle
        from matplotlib.patches import Polygon as MP

        fig, ax = plt.subplots(figsize=(6, 6), dpi=130)
        ax.add_patch(plt.Rectangle(W.bounds[:2], a.size, a.size, fc="#6b4e2e", ec="none"))
        for q in model["gnd"]:
            ax.add_patch(MP(q, fc="#e8c547", ec="none"))
        for v in model["nets"].values():
            for q in v:
                ax.add_patch(MP(q, fc="#e0412f", ec="none"))
        for q in model["l2_holes"]:
            ax.add_patch(MP(q, fc="none", ec="c", ls=":"))
        for v in model["vias"]:
            ax.add_patch(Circle(v[:2], 0.16, fc="k"))
        for q in probes:
            ax.plot(*q, "w+", ms=10)
        ax.set_xlim(W.bounds[0], W.bounds[2])
        ax.set_ylim(W.bounds[1], W.bounds[3])
        ax.set_aspect("equal")
        ax.set_title(
            f"plane-pair window {model['board']} (U1 frame mm); + sources/probes", fontsize=8
        )
        plt.savefig(a.plot, bbox_inches="tight")


if __name__ == "__main__":
    main()
