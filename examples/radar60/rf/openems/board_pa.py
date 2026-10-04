"""PA-corner models (D14, E1-PA) for pa_sim.py: the TX1 or the RX4 launch with about 3 mm of its
feed and the macro's VOUT_PA feed beside it, cut from a zone-filled macro board
(board_export.py with via nets) and the generator's paths. One model per line, so each line's
lead runs straight to the box's edge without passing the other's copper.

- Kept: the L1 GND fill as KiCad filled it; the U1 lands in the box (their own nets); TX1 and
  RX4 from their ball lands to a cut `--feed-len` beyond P0 (generator path), then a synthetic
  straight fenced GCPW lead north to the box's top edge (an MSL port's lead; the board's copper
  and vias inside that lead band are replaced); the PA feed's copper (1V0_PA pads) and vias; the
  GND vias; the L2 apertures (launch cut-outs, PA anti-pads: holes of the In1.Cu fill).
- Dropped: every other net's copper in the box (its gap is refilled with GND, as
  board_feeds.py does), and the rest of TX1 and RX4 beyond the cut.
- Ports (pa_sim.py): lumped ports at the two ball lands (L1 land to L3 through the L2 cut-out,
  50 ohm; the vendor boundary Pb) and MSL ports on the leads, the measurement plane at the cut.
  The PA vias end on L4 (short: the bottom caps as an RF short), 0.1 mm above it (open) or on a
  50 ohm lumped port to L4 (the island's port: its coupling to TX1 and RX4).

  python openems/board_pa.py BOARD.json RECORD.json OUT.json --line TX1|RX4 [--feed-len 3.0]
      [--plot PNG]

A board without the PA feed (rfmacro.variants s3b-pa0) gives the baseline. Needs shapely and
rfmacro (the record's parameters rebuild the paths).
"""

import argparse
import json
import math
import os
import sys

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get("RFMACRO_ROOT", os.path.dirname(HERE)))
from board_ant import holefree, mp, parts, track_poly  # noqa: E402

W50, GAP, FOFF, PITCH = 0.200, 0.200, 0.50, 0.45


def path_upto(path, s_cut):
    """Centreline points of a generator path up to arc length s_cut, and the heading there."""
    from rfmacro.geom import path_samples

    pts, head = [], None
    for q, h, s in path_samples(path, 0.01):
        if s > s_cut + 1e-9:
            break
        pts.append(q)
        head = h
    return pts, head


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("record")
    ap.add_argument("out")
    ap.add_argument("--line", required=True, choices=["TX1", "RX4"])
    ap.add_argument("--feed-len", type=float, default=3.0, help="feed kept beyond P0 (mm)")
    ap.add_argument("--plot")
    a = ap.parse_args()
    from rfmacro import macro as M

    d = json.load(open(a.board))
    rec = json.load(open(a.record))
    ov = {k: v for k, v in rec["params"].items() if k in rec.get("overrides", {})}
    ov["variant"] = rec["params"]["variant"]
    mc = M.build(ov)
    lines = (a.line,)
    cut, head, cpts = {}, {}, {}
    for n in lines:
        f = mc.feeds[n]
        s_cut = f.marks["P0"][1] + a.feed_len
        pts, h = path_upto(f, s_cut)
        # the lead runs north: move the cut to where the path heads north
        if abs(h - math.pi / 2) > 1e-3:
            raise SystemExit(f"{n}: the path at P0 + {a.feed_len} mm does not head north")
        cut[n], head[n], cpts[n] = pts[-1], h, pts
    # the box: the line's launch, its feed to the cut and the PA feed's copper (from the board
    # with the feed; a board without it takes the same box), the lead north to the top edge
    ref = mc.pa or M.build(dict(ov, pa_feed=True)).pa
    n = a.line
    cx, cy = cut[n]
    bx, by = mc.feeds[n].start
    pr = ref.rects
    if n == "TX1":  # west to the PA copper, east past the lead; up to the PA copper's top
        x0, x1 = min(r[0] for r in pr) - 0.85, cx + 0.85
        y0, y1 = by - 1.0, max(cy, max(r[3] for r in pr)) + 0.6
    else:  # RX4: west of its launch, east to just short of TX1's line (its fence row stays)
        x0, x1 = bx - 0.85, max(r[2] for r in pr) + 0.45
        y0, y1 = by - 1.0, cy + 1.4
    B = box(x0, y0, x1, y1)
    bands = {n: box(cut[n][0] - 0.85, cut[n][1], cut[n][0] + 0.85, y1 + 1) for n in lines}
    band = unary_union(list(bands.values()))
    zone = B.difference(band)

    # kept copper: the line up to its cut, its lead, the PA pads, the U1 lands
    nets = {}
    for n in lines:
        g = LineString(cpts[n]).buffer(W50 / 2, cap_style="flat", resolution=16)
        x = cut[n][0]
        g = g.union(box(x - W50 / 2, cut[n][1] - 0.005, x + W50 / 2, y1 + 0.01))
        nets[n] = g
    lands = {}
    for pd in d["pads"]:
        if pd["layer"] != "F.Cu":
            continue
        q = mp(pd["poly"])
        if not B.intersects(q):
            continue
        if pd["net"].startswith("1V0_"):
            nets.setdefault(pd["net"], Polygon())
            nets[pd["net"]] = nets[pd["net"]].union(q)
        elif pd["ref"] == "U1" and pd["net"] != "GND":
            if pd["net"] in lines:
                nets[pd["net"]] = nets[pd["net"]].union(q)
                lands[pd["net"]] = [round(q.centroid.x, 5), round(q.centroid.y, 5)]
            else:
                nets.setdefault(pd["net"], Polygon())
                nets[pd["net"]] = nets[pd["net"]].union(q)
    # dummy loads in the box stay as built (run-in and signal land; 50 ohm lumped to L2)
    loads = []
    for pd in d["pads"]:
        if pd["layer"] != "F.Cu" or not pd["ref"].startswith("RT") or pd["net"] == "GND":
            continue
        q = mp(pd["poly"])
        if zone.contains(q.centroid):
            n = pd["net"]
            g = unary_union([track_poly(t) for t in d["tracks"] if t["net"] == n] + [q])
            nets[n] = g.intersection(zone)
            c = q.centroid
            loads.append(dict(ref=pd["ref"], net=n, at=[round(c.x, 5), round(c.y, 5)], half=0.10))
    # GND: the fill in the zone, refilled where dropped copper was, the lead bands synthetic
    full = unary_union(
        [mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"]
    )
    gnd = full.intersection(zone)
    dropped = []
    for t in d["tracks"]:
        if t["layer"] != "F.Cu" or t["net"] in nets and t["net"] not in lines:
            continue
        dropped.append(track_poly(t))
    for pd in d["pads"]:
        if pd["layer"] == "F.Cu" and pd["ref"].startswith("ANT_") and pd["net"] != "GND":
            dropped.append(mp(pd["poly"]))
    holes = unary_union(
        [Polygon(h) for q in parts(full) for h in q.interiors if Polygon(h).area > 4.0]
    )
    kept = unary_union(list(nets.values()))
    refill = unary_union([q.buffer(GAP + 0.01) for q in dropped]).intersection(zone)
    refill = refill.difference(holes).difference(kept.buffer(GAP))
    gnd = gnd.union(refill)
    for n in lines:
        x = cut[n][0]
        bb = bands[n].intersection(B)
        gnd = gnd.union(
            bb.difference(box(x - W50 / 2 - GAP, cut[n][1] - 0.01, x + W50 / 2 + GAP, y1 + 1))
        )
    gnd = gnd.difference(kept.buffer(0.1))
    pwr = unary_union([v for k, v in nets.items() if k.startswith("1V0_")])
    gnd = gnd.difference(pwr.buffer(0.15)).buffer(0)  # PWR class clearance
    for n in lines:  # the line's own gap is the board's
        gnd = gnd.difference(LineString(cpts[n]).buffer(W50 / 2 + GAP * 0.999, cap_style="flat"))
    gnd = unary_union([q for q in parts(gnd.intersection(B)) if q.area > 1e-4])

    # vias: GND (board, outside the lead bands; posts L3-L1) and the PA vias; lead fence rows
    gvias, pvias = [], []
    for v in d["vias"]:
        pt = Point(v[0], v[1])
        if not B.contains(pt):
            continue
        net = v[4] if len(v) > 4 else "GND"
        if net == "GND":
            if not band.contains(pt):
                gvias.append([round(v[0], 5), round(v[1], 5), v[3]])
        elif net.startswith("1V0_"):
            pvias.append([round(v[0], 5), round(v[1], 5), v[3], v[2]])
    for n in lines:
        x = cut[n][0]
        y = cut[n][1] + 0.225
        while y < y1 - 0.1:
            for xv in (x - FOFF, x + FOFF):
                gvias.append([round(xv, 5), round(y, 5), 0.15])
            y += PITCH
    pads = unary_union([Point(v[0], v[1]).buffer(0.16, resolution=8) for v in gvias])
    gnd = gnd.union(pads.intersection(gnd.buffer(0.2)))
    l2 = unary_union([mp(f["poly"]) for f in d["fill"] if f["layer"] == "In1.Cu"])
    l2_holes = B.difference(l2)
    model = dict(
        model="pa",
        board=a.board.split("/")[-1],
        pa=bool(pvias),
        box=[round(v, 5) for v in (x0, x1, y0, y1)],
        gnd=holefree(gnd),
        nets={k: holefree(v.intersection(B)) for k, v in nets.items() if not v.is_empty},
        vias=gvias,
        pa_vias=pvias,
        pa_antipad_r=round(ref.pad / 2 + float(rec["params"]["pa_clear"]), 4),
        l2_holes=holefree(l2_holes),
        land_ports=[dict(name=f"{n}.Pb", net=n, at=lands[n], half=0.10) for n in lines],
        loads=loads,
        ports=[
            dict(name=f"{n}.Pf", net=n, at=[round(cut[n][0], 5), round(cut[n][1], 5)], dir="y")
            for n in lines
        ],
        feed_len_mm=a.feed_len,
    )
    json.dump(model, open(a.out, "w"))
    print(
        a.out,
        "box",
        model["box"],
        "gnd polys",
        len(model["gnd"]),
        "vias",
        len(gvias),
        "pa vias",
        len(pvias),
        "nets",
        sorted(model["nets"]),
        "l2 holes",
        len(model["l2_holes"]),
    )
    if a.plot:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle
        from matplotlib.patches import Polygon as MP

        fig, ax = plt.subplots(figsize=(8, 8 * (y1 - y0) / (x1 - x0)), dpi=130)
        ax.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0, fc="#6b4e2e", ec="none"))
        for q in model["gnd"]:
            ax.add_patch(MP(q, fc="#e8c547", ec="none"))
        for k, v in model["nets"].items():
            c = "#e0412f" if k in lines else ("#3aa655" if k.startswith("1V0") else "#aaaaaa")
            for q in v:
                ax.add_patch(MP(q, fc=c, ec="none"))
        for q in model["l2_holes"]:
            ax.add_patch(MP(q, fc="none", ec="c", ls=":", lw=0.6))
        for v in gvias:
            ax.add_patch(Circle(v[:2], 0.16, fc="k"))
        for v in pvias:
            ax.add_patch(Circle(v[:2], v[3] / 2, fc="#1b5e20", ec="w", lw=0.4))
        for pt in model["land_ports"] + model["ports"]:
            ax.plot(*pt["at"], "w+", ms=9)
            ax.annotate(
                pt["name"],
                pt["at"],
                color="w",
                fontsize=6,
                xytext=(3, 3),
                textcoords="offset points",
            )
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect("equal")
        ax.set_title(f"PA corner {model['board']} (U1 frame mm); green: 1V0_PA", fontsize=8)
        plt.savefig(a.plot, bbox_inches="tight")


if __name__ == "__main__":
    main()
