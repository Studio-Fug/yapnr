"""Feed models of the regenerated macro (and the as-built RX feeds) for feed_sim.py.

The copper of a zone-filled macro board (board_export.py, U1 frame) inside a box, fence vias as
PEC posts, the package side of P0 replaced by a straight fenced GCPW lead-in per line, P0 and P1
as ports, the column inputs running straight into the north PML (no patches). Also:

- `--model txa`: TX1, TX2 and TX3 (6 ports). The board is kept north of
  the lead-in band also west of P0 (the RXD5 dummy run-in and its load sit at x 5.2).
- `--model rx`: RX1-RX4 (8 ports); P0 heads north, so the lead-in band is south of P0.
- Dummy loads (0201, 50 ohm) become a 50 ohm lumped termination from the load's signal pad to L2
  (`loads`), the GND pad stays GND copper. Dummy copper is kept only where it connects to its load.

  python openems/board_feeds.py BOARD.json RECORD.json OUT.json --model txa|rx [--plot PNG]

Needs shapely (and matplotlib for --plot); RECORD.json is the macro's generated record.
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
ap.add_argument("--model", required=True)
ap.add_argument("--plot")
ap.add_argument("--tag", default="")
a = ap.parse_args()
d = json.load(open(a.board))
rec = json.load(open(a.record))
p = {k: v["at"] for k, v in rec["ports"].items() if "at" in v}
W50, GAP, LEAD = 0.200, 0.200, 5.2  # P0 sits 5.2 mm from U1's centre on both edges


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


if a.model == "txa":
    lines = ["TX1", "TX2", "TX3"]
    side = "west"
    BOX = (3.6, max(p[f"{n}.Pg"][0] for n in lines) + 1.6, -1.3, p["TX1.P1"][1] + 1.4)
    ZONE = dict(w=1.0, n=1.0, s=0.85, e=0.85)
elif a.model == "rx":
    lines = ["RX1", "RX2", "RX3", "RX4"]
    side = "south"
    xs = [p[f"{n}.P1"][0] for n in lines] + [p[f"{n}.P0"][0] for n in lines]
    BOX = (min(xs) - 1.6, max(xs) + 1.6, LEAD - 1.6, p["RX1.P1"][1] + 1.4)
    ZONE = dict(w=0.85, n=1.0, s=1.0, e=0.85)
else:
    raise SystemExit("model")
port_names = [f"{n}.{k}" for n in lines for k in ("P0", "P1")]
x0, x1, y0, y1 = BOX
B = box(x0, y0, x1, y1)

# lead-in band: the board is replaced by straight fenced GCPW between the box edge and P0
if side == "west":
    lead = [(n, p[f"{n}.P0"][1]) for n in lines]
    band = box(x0, y0, LEAD, min(y1, max(v for _, v in lead) + 0.65))
else:
    lead = [(n, p[f"{n}.P0"][0]) for n in lines]
    band = box(min(v for _, v in lead) - 0.65, y0, max(v for _, v in lead) + 0.65, LEAD)
board_zone = B.difference(band)

gnd = unary_union([mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"])
gnd = gnd.intersection(board_zone)
nets = {}
for t in d["tracks"]:
    if t["layer"] == "F.Cu":
        nets.setdefault(t["net"], []).append(track_poly(t))
load_pads = {}  # net -> (signal pad polygon, gnd pad polygon)
for pd in d["pads"]:
    if pd["layer"] != "F.Cu" or not pd["ref"].startswith("RL"):
        continue
    q = mp(pd["poly"])
    load_pads.setdefault(pd["ref"], {})[pd["net"]] = q
loads = []
for ref, pads in sorted(load_pads.items()):
    sig = [n for n in pads if n != "GND"][0]
    if not board_zone.contains(pads[sig].centroid):
        continue
    nets.setdefault(sig, []).append(pads[sig])
    gnd = gnd.union(pads["GND"].intersection(board_zone))
    c = pads[sig].centroid
    loads.append(dict(ref=ref, net=sig, at=[round(c.x, 5), round(c.y, 5)], half=0.10, R=50.0))
nets = {k: unary_union(v).intersection(board_zone) for k, v in nets.items()}

# column inputs: P1 -> box top must be the straight input line
for n in lines:
    px, py = p[f"{n}.P1"]
    straight = nets[n].intersection(box(px - W50, py, px + W50, y1)).area
    assert abs(straight - W50 * (y1 - py)) < 1e-3 * W50 * (y1 - py) + 1e-4, (n, straight)
# keep per net only the copper that connects to its own ports (active lines) or to its load
# (dummies); everything else (arms cut off by the box, nets of the other bank) is dropped and
# its gap is filled with GND
ppts = {n: Point(*p[n]) for n in port_names}
dropped_gaps = []
for n in list(nets):
    own = [ppts[k] for k in port_names if k.startswith(n + ".")]
    lp = [Point(*ld["at"]) for ld in loads if ld["net"] == n]
    pcs = parts(nets[n].intersection(B))
    keep = [q for q in pcs if any(q.distance(pp) < 0.05 for pp in own + lp)]
    for q in pcs:
        if q not in keep:
            dropped_gaps.append(q.buffer(GAP + 0.01))
    nets[n] = unary_union(keep) if keep else Polygon()
    if nets[n].is_empty:
        nets.pop(n)
if dropped_gaps:
    # the patch cut-outs (large holes of the whole board's GND pour) stay bare
    full = unary_union(
        [mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"]
    )
    cut = unary_union(
        [Polygon(h) for q in parts(full) for h in q.interiors if Polygon(h).area > 4.0]
    )
    fill = unary_union(dropped_gaps).intersection(board_zone).difference(cut)
    others = unary_union([v.buffer(GAP) for v in nets.values()])
    gnd = gnd.union(fill.difference(others)).buffer(0)

vias = [v for v in d["vias"] if board_zone.contains(Point(v[0], v[1]))]
# synthetic lead-in: straight GCPW for every line, GND band, fence rows between and beside them
for n, c in lead:
    if side == "west":
        nets[n] = nets.get(n, Polygon()).union(box(x0, c - W50 / 2, LEAD + 0.01, c + W50 / 2))
    else:
        nets[n] = nets.get(n, Polygon()).union(box(c - W50 / 2, y0, c + W50 / 2, LEAD + 0.01))
gband = band
for n, c in lead:
    if side == "west":
        gband = gband.difference(box(x0 - 1, c - W50 / 2 - GAP, LEAD, c + W50 / 2 + GAP))
    else:
        gband = gband.difference(box(c - W50 / 2 - GAP, y0 - 1, c + W50 / 2 + GAP, LEAD))
gnd = gnd.union(gband)
cs = sorted(c for _, c in lead)
rows = [cs[0] - 0.5] + [(u + v) / 2 for u, v in zip(cs, cs[1:])] + [cs[-1] + 0.5]
t = LEAD - 0.225
while t > (x0 if side == "west" else y0):
    for r in rows:
        if side == "west" and y0 < r < y1:
            vias.append([t, r, 0.30, 0.15])
        if side == "south" and x0 < r < x1:
            vias.append([r, t, 0.30, 0.15])
    t -= 0.45

gnd = gnd.intersection(B)
pads = unary_union([Point(v[0], v[1]).buffer(v[2] / 2, resolution=8) for v in vias])
gnd = gnd.union(pads.intersection(gnd.buffer(0.2)))


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


INNER = box(x0 + ZONE["w"], y0 + ZONE["s"], x1 - ZONE["e"], y1 - ZONE["n"])


def split(g):
    return holefree(g.intersection(INNER)), holefree(g.difference(INNER))


def pdir(n):
    if n.endswith("P1"):
        return "y"  # lead from the north edge
    return "x" if side == "west" else "ys"


model = dict(
    model=a.model + a.tag,
    variant="after",
    box=BOX,
    p0x=LEAD,
    zone=ZONE,
    inner=list(INNER.bounds),
    gnd=split(gnd),
    nets={k: split(v.intersection(B)) for k, v in nets.items() if not v.intersection(B).is_empty},
    vias=[[round(v[0], 5), round(v[1], 5), v[3]] for v in vias if B.contains(Point(v[0], v[1]))],
    l2_windows=[],
    loads=loads,
    ports=[dict(name=n, net=n.split(".")[0], at=p[n], dir=pdir(n)) for n in port_names],
    slivers=[],
)
json.dump(model, open(a.out, "w"))
print(
    a.out,
    "box",
    [round(v, 3) for v in BOX],
    "gnd pieces",
    [len(q) for q in model["gnd"]],
    "vias",
    len(model["vias"]),
    "nets",
    sorted(model["nets"]),
    "loads",
    [(ld["ref"], ld["at"]) for ld in loads],
)

if a.plot:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    from matplotlib.patches import Polygon as MP

    fig, ax = plt.subplots(figsize=(9, 9 * (y1 - y0) / (x1 - x0)), dpi=130)
    ax.set_facecolor("#6b4e2e")
    for q in model["gnd"][0] + model["gnd"][1]:
        ax.add_patch(MP(q, fc="#e8c547", ec="none"))
    for k, v in model["nets"].items():
        for q in v[0] + v[1]:
            ax.add_patch(
                MP(
                    q,
                    fc=(
                        "#e0412f"
                        if k.startswith("TX") and "D" not in k
                        else "#3070e0" if k.startswith("RX") and "D" not in k else "#aaaaaa"
                    ),
                    ec="none",
                )
            )
    ib = model["inner"]
    ax.add_patch(
        plt.Rectangle(ib[:2], ib[2] - ib[0], ib[3] - ib[1], fc="none", ec="w", ls="--", lw=0.6)
    )
    for v in model["vias"]:
        ax.add_patch(Circle(v[:2], v[2] / 2, fc="k"))
    for ld in loads:
        ax.plot(*ld["at"], "c*", ms=8)
    for pt in model["ports"]:
        ax.plot(*pt["at"], "w+", ms=10)
        ax.annotate(
            pt["name"], pt["at"], color="w", fontsize=6, xytext=(3, 3), textcoords="offset points"
        )
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.set_title(
        f"{model['model']} ({a.board.split('/')[-1]}), U1 frame mm; dashed: PML zone edge; star: 50 ohm load",
        fontsize=8,
    )
    plt.savefig(a.plot, bbox_inches="tight")
