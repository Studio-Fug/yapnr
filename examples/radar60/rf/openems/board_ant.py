"""Radiating models of the regenerated macro (TX bank) for ant_sim.py: one column with its new
entry ("cell"), or the whole TX bank ("bank") with or without terminated dummy columns.

The geometry is the zone-filled KiCad board (board_export.py, U1 frame) inside a finite board
that ends 0.5 lambda0 beyond the copper, as in stage 2's column model (openems/column_sim.py): L1 GND pour as
filled by KiCad, the column copper (patches, divider, input), the run-in from Pg with its fence
pairs, the stitch ring and the pour vias, the L2 windows (holes of the In1.Cu fill). South of Pg
each fed column's real equalizer is replaced by a straight fenced GCPW lead (1.2 mm port lead plus
the board margin), so every column is driven through the same entry; the old feed's gap becomes
GND. Dummy loads become 50 ohm lumped terminations (signal pad to L2).

  python openems/board_ant.py BOARD.json RECORD.json OUT.json --model cell|col0 --col TX2
  python openems/board_ant.py BOARD.json RECORD.json OUT.json --model bank [--plot PNG]

Needs shapely (and matplotlib for --plot).

cell: only the named column is kept; every other column's copper is removed (its gap outside the
cut-out becomes GND, its vias stay; inside the cut-out the substrate is bare), like stage 2's
isolated column but with the pour, ring, run-in and entry.
"""

import argparse
import json
import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

ap = argparse.ArgumentParser()
ap.add_argument("board")
ap.add_argument("record")
ap.add_argument("out")
ap.add_argument("--model", required=True, choices=["cell", "bank", "col0"])
ap.add_argument("--col", default="TX2")
ap.add_argument("--bank", default="TX")
ap.add_argument("--plot")
a = ap.parse_args()
d = json.load(open(a.board))
rec = json.load(open(a.record))
P = {k: v["at"] for k, v in rec["ports"].items() if "at" in v}
cols = rec["columns"]
cut = rec["cutouts_u1_mm"][a.bank]
CUT = box(*cut)
W50, GAP, LPORT = 0.200, 0.200, 1.2
LAM0 = 299792458.0 / 62e9 * 1e3


def mp(poly):
    return unary_union([Polygon(r, h) for r, h in poly if len(r) >= 3]).buffer(0)


def parts(g):
    if g.is_empty:
        return []
    return list(g.geoms) if hasattr(g, "geoms") else [g]


def track_poly(t):
    if t["kind"] == "PCB_ARC":
        (x0, y0), (xm, ym), (x1, y1) = t["start"], t["mid"], t["end"]
        a_ = x0 * (ym - y1) - y0 * (xm - x1) + xm * y1 - x1 * ym
        b_ = (x0**2 + y0**2) * (y1 - ym) + (xm**2 + ym**2) * (y0 - y1) + (x1**2 + y1**2) * (ym - y0)
        c_ = (x0**2 + y0**2) * (xm - x1) + (xm**2 + ym**2) * (x1 - x0) + (x1**2 + y1**2) * (x0 - xm)
        cx, cy = -b_ / (2 * a_), -c_ / (2 * a_)
        r = math.hypot(x0 - cx, y0 - cy)
        t0, tm, t1 = (math.atan2(q[1] - cy, q[0] - cx) for q in (t["start"], t["mid"], t["end"]))

        def unwrap(u, ref):
            while u - ref > math.pi:
                u -= 2 * math.pi
            while ref - u > math.pi:
                u += 2 * math.pi
            return u

        tm = unwrap(tm, t0)
        t1 = unwrap(t1, tm)
        n = max(8, int(abs(t1 - t0) * r / 0.01))
        pts = [
            (cx + r * math.cos(t0 + (t1 - t0) * k / n), cy + r * math.sin(t0 + (t1 - t0) * k / n))
            for k in range(n + 1)
        ]
        return LineString(pts).buffer(t["w"] / 2, cap_style="round", resolution=16)
    return LineString([t["start"], t["end"]]).buffer(t["w"] / 2, cap_style="round", resolution=16)


bank_cols = [c for c in cols if c.startswith(a.bank)]
active = [c for c in bank_cols if not cols[c]["dummy"]]
keep = [a.col] if a.model in ("cell", "col0") else bank_cols
fed = [c for c in keep if not cols[c]["dummy"]]

copper = {}
for t in d["tracks"]:
    if t["layer"] == "F.Cu":
        copper.setdefault(t["net"], []).append(track_poly(t))
for pd in d["pads"]:
    if (
        pd["layer"] == "F.Cu"
        and (pd["ref"].startswith("ANT_") or pd["ref"].startswith("RL"))
        and pd["net"] != "GND"
    ):
        copper.setdefault(pd["net"], []).append(mp(pd["poly"]))
copper = {k: unary_union(v) for k, v in copper.items()}
load_pads = {}
for pd in d["pads"]:
    if pd["layer"] == "F.Cu" and pd["ref"].startswith("RL"):
        load_pads.setdefault(pd["ref"], {})[pd["net"]] = mp(pd["poly"])

# fed columns: real copper north of Pg + straight lead from the port start up to Pg
# col0 (control): stage 2's isolated column, port at P1 with a 1.2 mm lead, no pour, no vias
pname = "P1" if a.model == "col0" else "Pg"
pg = {c: P[f"{c}.{pname}"] for c in fed}
yport = {c: pg[c][1] - LPORT for c in fed}
nets, old = {}, []
for c in keep:
    g = copper[c]
    if c in fed:
        x, y = pg[c]
        north = g.intersection(box(-1e3, y, 1e3, 1e3))
        old.append(g.intersection(box(-1e3, -1e3, 1e3, y)))
        g = north.union(box(x - W50 / 2, yport[c], x + W50 / 2, y + 0.01))
    nets[c] = g
removed = [copper[n] for n in copper if n.startswith(a.bank) and n not in keep]
# board extent: copper bbox (fed leads included) + 0.5 lambda0 (stage 2's rule)
cb = unary_union(list(nets.values())).bounds
m_sub = 0.5 * LAM0
SUB = box(cb[0] - m_sub, cb[1] - m_sub, cb[2] + m_sub, cb[3] + m_sub)

gnd = unary_union([mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"])
keep_gap = unary_union([v.buffer(GAP) for v in nets.values()])
fill = unary_union([q.buffer(GAP + 0.01) for q in old] + [q.buffer(GAP + 0.01) for q in removed])
fill = fill.difference(CUT).difference(keep_gap)
for c in fed:  # the old equalizer's gap ends at Pg; the run-in's gap north of Pg is untouched
    pass
gnd = gnd.union(fill)
leadgaps = []
for c in fed:
    x, y = pg[c]
    lg = box(x - W50 / 2 - GAP, yport[c] - GAP, x + W50 / 2 + GAP, y + 0.001)
    leadgaps.append(lg)
    gnd = gnd.difference(lg)
for c in keep:  # dummy loads: the GND pad joins the pour
    for ref, pads in load_pads.items():
        if c in pads:
            gnd = gnd.union(pads["GND"])
gnd = gnd.buffer(0).intersection(SUB)
gnd = unary_union([q for q in parts(gnd) if q.area > 1e-4])
if a.model == "col0":
    gnd = Polygon()

vias = [v for v in d["vias"] if SUB.contains(Point(v[0], v[1]))] if a.model != "col0" else []
LG = unary_union(leadgaps)
vias = [v for v in vias if Point(v[0], v[1]).distance(LG) > v[2] / 2 + 0.005]
added = 0
for c in fed:
    x, y = pg[c]
    for k in (1, 2, 3):
        yy = y + 0.05 - 0.45 * k
        if yy < yport[c] - GAP:
            break
        for xx in (x - 0.5, x + 0.5) if a.model != "col0" else ():
            if all(math.hypot(xx - v[0], yy - v[1]) > 0.40 for v in vias):
                vias.append([xx, yy, 0.32, 0.15])
                added += 1
if vias:
    gnd = gnd.union(
        unary_union([Point(v[0], v[1]).buffer(v[2] / 2, resolution=8) for v in vias]).intersection(
            gnd.buffer(0.2)
        )
    )

# L2 windows: holes of the In1.Cu fill under the kept patches
l2 = unary_union([mp(f["poly"]) for f in d["fill"] if f["layer"] == "In1.Cu"])
pads_kept = unary_union(
    [
        mp(pd["poly"])
        for pd in d["pads"]
        if pd["layer"] == "F.Cu" and pd["ref"].startswith("ANT_") and pd["net"] in keep
    ]
)
windows = []
for q in parts(l2):
    for h in q.interiors:
        hp = Polygon(h)
        if 1.0 < hp.area < 5.0 and hp.intersects(pads_kept):
            bx = hp.bounds
            assert abs(hp.area - (bx[2] - bx[0]) * (bx[3] - bx[1])) < 1e-3, (
                "window not a rectangle",
                bx,
            )
            windows.append([round(v, 5) for v in bx])
assert len(windows) == 2 * len(keep), (len(windows), keep)

loads = []
for ref, pads in sorted(load_pads.items()):
    sig = [n for n in pads if n != "GND"][0]
    if sig in keep:
        cc = pads[sig].centroid
        loads.append(dict(ref=ref, net=sig, at=[round(cc.x, 5), round(cc.y, 5)], half=0.10, R=50.0))


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


model = dict(
    model=a.model,
    board=a.board.split("/")[-1],
    col=a.col if a.model in ("cell", "col0") else None,
    columns=keep,
    fed=fed,
    sub=[round(v, 5) for v in SUB.bounds],
    cbox=[
        round(cb[0] - 0.2, 5),
        round(cb[1] - 0.2, 5),
        round(cb[2] + 0.2, 5),
        round(cb[3] + 0.2, 5),
    ],
    cutout=cut,
    gnd=holefree(gnd),
    nets={k: holefree(v) for k, v in nets.items()},
    vias=[[round(v[0], 5), round(v[1], 5), v[3]] for v in vias],
    windows=windows,
    loads=loads,
    ports=[dict(name=f"{c}.{pname}", net=c, at=pg[c], start_y=yport[c]) for c in fed],
    phase_centres={c: cols[c]["phase_centre"] for c in keep},
    added_lead_vias=added,
)
json.dump(model, open(a.out, "w"))
print(
    a.out,
    "sub",
    model["sub"],
    "cbox",
    model["cbox"],
    "gnd polys",
    len(model["gnd"]),
    "vias",
    len(vias),
    "(+%d lead)" % added,
    "windows",
    len(windows),
    "loads",
    [ld["ref"] for ld in loads],
    "fed",
    fed,
)

if a.plot:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    from matplotlib.patches import Polygon as MP

    s = model["sub"]
    fig, ax = plt.subplots(figsize=(9, 9 * (s[3] - s[1]) / (s[2] - s[0])), dpi=130)
    ax.set_facecolor("white")
    ax.add_patch(plt.Rectangle(s[:2], s[2] - s[0], s[3] - s[1], fc="#6b4e2e", ec="none"))
    for q in model["gnd"]:
        ax.add_patch(MP(q, fc="#e8c547", ec="none"))
    for w in windows:
        ax.add_patch(
            plt.Rectangle((w[0], w[1]), w[2] - w[0], w[3] - w[1], fc="none", ec="c", ls=":", lw=0.8)
        )
    for k, v in model["nets"].items():
        for q in v:
            ax.add_patch(MP(q, fc=("#aaaaaa" if cols[k]["dummy"] else "#e0412f"), ec="none"))
    for v in model["vias"]:
        ax.add_patch(Circle(v[:2], v[2] / 2, fc="k"))
    for ld in loads:
        ax.plot(*ld["at"], "c*", ms=8)
    for pt in model["ports"]:
        ax.plot(*pt["at"], "w+", ms=10)
        ax.plot([pt["at"][0]] * 2, [pt["start_y"], pt["at"][1]], "w-", lw=0.6)
        ax.annotate(
            pt["name"], pt["at"], color="w", fontsize=6, xytext=(3, -8), textcoords="offset points"
        )
    cbx = model["cbox"]
    ax.add_patch(
        plt.Rectangle(cbx[:2], cbx[2] - cbx[0], cbx[3] - cbx[1], fc="none", ec="m", ls="--", lw=0.6)
    )
    ax.set_xlim(s[0] - 0.2, s[2] + 0.2)
    ax.set_ylim(s[1] - 0.2, s[3] + 0.2)
    ax.set_aspect("equal")
    ax.set_title(
        f"{a.model} {model['col'] or ''} ({model['board']}), U1 frame mm; magenta: fine-mesh box; "
        "cyan dotted: L2 windows; star: 50 ohm load",
        fontsize=7,
    )
    plt.savefig(a.plot, bbox_inches="tight")
