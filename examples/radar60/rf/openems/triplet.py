"""Triplet models for ant_sim.py, built from the generator (no KiCad fill): three columns of the
bank's cell side by side, the centre one driven at its Pg, the two neighbours terminated by
matched ports at theirs, each with the macro's straight run-in and entry, the bank lattice (run-in
pairs, ring sites), the two ring rows round its own cut-out, the open pour with its D15 grid and
the L2 windows (none for K1), the K2 posts. This is CP-A's environment (the column between two
neighbours) with the real L2-L3 extent: the bondply runs to the substrate's edge and the ring
bounds the cavity under the three columns.

Stage 3b uses it for the C1 corporate retune (E1-C1a/E2), the D5 series-fed points (E1-SER) and
the L2-L3 cavity options (E1-CAV): only the column parameters change between points, so a sweep
needs no board fill. Every point is checked by building the cell with rfmacro's own rules; the
boards of the DOE corners (rfmacro.variants s3b-C1lo/hi, s3b-K1/K2, s3b-SER) are DRC-checked.

  python3 openems/triplet.py OUT.json [--set key=json ...] [--margin 2.42] [--plot PNG]
      [--mesh-like OTHER.json]   # add OTHER's window edges as mesh lines (K0/K2/K1 on one mesh)

Needs shapely (and matplotlib for --plot). Frame: the RX bank's (inputs on +x, unmirrored);
the centre column's input at x = 0, its entry at y = 0.
"""

import argparse
import json
import math
import os
import sys
from types import SimpleNamespace

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

sys.path.insert(
    0, os.environ.get("RFMACRO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from rfmacro import cavity as cavity_mod  # noqa: E402
from rfmacro import dims as dims_mod  # noqa: E402
from rfmacro import macro as M  # noqa: E402
from rfmacro.params import D_LATTICE, RULES, resolve  # noqa: E402
from rfmacro.vias import lattice_sites, ring_points  # noqa: E402

LAM0 = 299792458.0 / 62e9 * 1e3
LPORT = 1.2  # MSL port lead below Pg (board_ant.py)
NAMES = ["T0", "T1", "T2"]


def seg_poly(sg):
    """Copper of one path segment (round caps, so consecutive segments join)."""
    if sg.kind == "line":
        return LineString([sg.p0, sg.p1]).buffer(sg.width / 2, cap_style="round", resolution=16)
    n = max(8, int(abs(sg.sweep) * sg.radius / 0.01))
    pts = [
        (
            sg.center[0] + sg.radius * math.cos(sg.a0 + sg.sweep * k / n),
            sg.center[1] + sg.radius * math.sin(sg.a0 + sg.sweep * k / n),
        )
        for k in range(n + 1)
    ]
    return LineString(pts).buffer(sg.width / 2, cap_style="round", resolution=16)


def holefree(g):
    parts = list(g.geoms) if hasattr(g, "geoms") else [g]
    out = []
    while parts:
        q = parts.pop()
        if q.is_empty or q.area < 1e-6:
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
            r = q.intersection(half)
            parts += [
                x for x in (list(r.geoms) if hasattr(r, "geoms") else [r]) if isinstance(x, Polygon)
            ]
    return [list(map(list, q.simplify(0.001).exterior.coords))[:-1] for q in out]


def build(ov, margin, centre_only=False):
    p = resolve(ov)
    d = dims_mod.compute(p)
    ru = M.Rules(p, d)
    dl = D_LATTICE
    e = 0.0
    pg = e - ru.lout
    xin = {n: (k - 1) * dl for k, n in enumerate(NAMES)}
    oy = e + ru.lin - ru.p1_y
    cols = {n: M.make_column(f"COL_{n}", n, (x - ru.in_x, oy), False, p, d) for n, x in xin.items()}
    cut = ru.cut(list(xin.values()), e, False)
    fake = SimpleNamespace(
        banks={"RX": dict(E=e, inputs=dict(xin))}, cutouts={"RX": cut}, params=p, dims=d
    )
    # copper: column paths and patches, run-ins from the port lead's start through the entry to P1
    nets = {}
    lines = []
    fed = ["T1"] if centre_only else list(NAMES)
    for n, c in cols.items():
        if n not in fed:
            continue  # centre only: the neighbours' copper is absent (bare in the cut-out)
        g = [seg_poly(sg) for q in M._place_paths(c) for sg in q.segs]
        g += [Polygon(poly) for poly in c.patches]
        x = xin[n]
        g.append(box(x - ru.w50 / 2, pg - LPORT, x + ru.w50 / 2, c.p1[1] + 0.01))
        nets[n] = unary_union(g)
        lines.append(LineString([(x, pg - LPORT - ru.gap), (x, c.p1[1])]))
        lines += [LineString([sg.p0, sg.p1]) for q in M._place_paths(c) for sg in q.segs]
    copper = unary_union(list(nets.values()))
    cb = copper.bounds
    sub = (cut[0] - margin, pg - LPORT - margin, cut[2] + margin, cut[3] + margin)
    SUB = box(*sub)
    # L1 GND: the substrate minus the cut-out, the GCPW channels of the run-ins and leads (the
    # lead's gap ends a gap below the port's start, as board_ant.py's)
    chan = unary_union(
        [box(xin[n] - ru.lchan, pg - LPORT - ru.gap, xin[n] + ru.lchan, e + 0.001) for n in fed]
    )
    gnd = SUB.difference(box(*cut)).difference(chan)
    # vias: the lattice, the two ring rows, the fence rows of the leads, the grid; K2 posts
    pr = RULES["via_fence"][1] / 2
    vias = []

    def ok(q, keep=0.43):
        pt = Point(q)
        if not SUB.buffer(-0.2).contains(pt):
            return False
        if any(math.dist(q, v) < keep - 1e-9 for v in vias):
            return False
        return all(ln.distance(pt) >= ru.site_min - 1e-6 for ln in lines if ln.distance(pt) < 1.0)

    for q, role in lattice_sites(fake, ru, "RX"):
        vias.append(q)
    ri = float(p["ring_inset"])
    for t, pitch in ((ri, 0.45), (ru.B, float(p["stitch_pitch"]))):
        last = None
        for s, q in ring_points(cut, t, 0.01):
            if q[1] < cut[1]:
                continue  # the south side: the lattice's run-in pairs and ring sites
            if last is None or s - last >= pitch - 1e-9:
                if ok(q):
                    vias.append(q)
                    last = s
    for x in xin.values():  # the leads' fence rows below the run-in pairs
        y = e - ru.B - ru.pitch
        while y > sub[1] + 0.2:
            for xv in (x - ru.foff, x + ru.foff):
                if ok((xv, y)):
                    vias.append((xv, y))
            y -= ru.pitch
    g = float(p["stitch_grid"])
    for i in range(int(math.floor(sub[0] / g)), int(math.ceil(sub[2] / g)) + 1):
        for j in range(int(math.floor(sub[1] / g)), int(math.ceil(sub[3] / g)) + 1):
            q = (i * g, j * g)
            if not gnd.buffer(-pr).contains(Point(q)):
                continue
            if Point(q).distance(box(*cut)) < ru.B - 1e-6 and q[1] > pg:
                continue  # only the lattice in the guard band
            if ok(q):
                vias.append(q)
    posts = []
    if p["l23_cavity"] == "K2":
        loc = cavity_mod.cell_posts(fake, ru)
        for c in cols.values():
            for q in loc:
                u = M._xf(q, c.origin, c.mirror)
                if all(math.dist(u, v) >= 0.43 - 1e-9 for v in vias + posts):
                    posts.append(u)
    allv = vias + posts
    gnd = gnd.union(
        unary_union([Point(v).buffer(pr, resolution=8) for v in allv]).intersection(gnd.buffer(0.2))
    )
    windows = []
    for n, c in cols.items():
        if n not in fed:
            continue
        for w in c.windows:
            xs, ys = [q[0] for q in w], [q[1] for q in w]
            windows.append(
                [round(min(xs), 5), round(min(ys), 5), round(max(xs), 5), round(max(ys), 5)]
            )
    model = dict(
        model="triplet",
        board="generator",
        col="T1",
        banks=["RX"],
        columns=fed,
        fed=fed,
        loads_mode="ports",
        overrides=ov,
        params={
            k: p[k]
            for k in (
                "fullwave_l_scale",
                "inset",
                "w35",
                "t_y",
                "div_l_scale",
                "column",
                "l23_cavity",
                "windows",
                "d15",
                "ser_link_scale",
                "ser_link_w",
                "variant",
            )
        },
        patch=dict(w=d["patch"]["w"], l=d["patch"]["l"], inset=d["patch"]["inset"]),
        arms=cols["T1"].arm_lengths,
        sub=[round(v, 5) for v in sub],
        cbox=[
            round(cb[0] - 0.2, 5),
            round(cb[1] - 0.2, 5),
            round(cb[2] + 0.2, 5),
            round(cb[3] + 0.2, 5),
        ],
        cutouts={"RX": [round(v, 5) for v in cut]},
        periods={},
        gnd=holefree(gnd),
        nets={k: holefree(v) for k, v in nets.items()},
        vias=[[round(v[0], 5), round(v[1], 5), RULES["via_fence"][0]] for v in allv],
        posts=len(posts),
        shorts=[],
        windows=windows,
        loads=[],
        load_mesh=[],
        ports=[dict(name=f"{n}.Pg", net=n, at=[xin[n], pg], start_y=pg - LPORT) for n in fed],
        phase_centres={n: list(c.origin) for n, c in cols.items() if n in fed},
    )
    return model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--set", action="append", default=[], help="parameter override key=json")
    ap.add_argument("--margin", type=float, default=LAM0 / 2)
    ap.add_argument(
        "--mesh-like", help="another triplet whose window edges are added as mesh lines"
    )
    ap.add_argument(
        "--centre-only",
        action="store_true",
        help="the centre column alone in the triplet's cut-out and lattice (the DOE cell)",
    )
    ap.add_argument("--plot")
    a = ap.parse_args()
    ov = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        ov[k] = json.loads(v)
    m = build(ov, a.margin, a.centre_only)
    if a.centre_only:
        m["model"] = "cell3"
    if a.mesh_like:
        o = json.load(open(a.mesh_like))
        m["mesh_lines"] = dict(
            x=sorted({w[0] for w in o["windows"]} | {w[2] for w in o["windows"]}),
            y=sorted({w[1] for w in o["windows"]} | {w[3] for w in o["windows"]}),
        )
    json.dump(m, open(a.out, "w"))
    print(
        a.out,
        "sub",
        m["sub"],
        "vias",
        len(m["vias"]),
        "posts",
        m["posts"],
        "windows",
        len(m["windows"]),
        "patch",
        m["patch"],
    )
    if a.plot:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from board_ant import plot

        plot(m, {n: dict(dummy=False) for n in NAMES}, a.plot)


if __name__ == "__main__":
    main()
