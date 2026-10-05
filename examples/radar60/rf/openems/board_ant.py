"""Radiating models of the regenerated macro for ant_sim.py: one column with its entry ("cell"),
the banks with or without terminated dummy columns ("bank"), or stage 2's isolated column as a
control ("col0").

North of each bank's Pg the geometry is the zone-filled KiCad board (board_export.py, U1 frame):
L1 GND pour as filled by KiCad, the column copper (patches, divider, input), the run-ins with
their fence pairs, the stitch ring and the pour vias, the L2 windows (holes of the In1.Cu fill).
South of Pg every column gets the same synthetic surroundings (review 2026-10-04: the old
equalizers' vias had been left in, so each fed column saw a different lead): a straight fenced
GCPW lead from the port (1.2 mm) for a fed column, the board's own load cell (the 0201 land pair
and its five vias, identical by G7) for a dummy, and a via lattice that repeats at the column
pitch (rows 0.45 mm apart from Pg - 0.40 down, columns at the lead's fence offset and on the
period boundaries), with plain GND and a d/4 grid beyond the bank. Dummy loads are 50 ohm lumped
terminations (signal land to L2), or shorted / left open (`--loads`).

The finite board ("bank"): the selected banks' L1 cut-outs grown by one lambda0 (62 GHz) west,
east and south, and north to the real board edge or one lambda0, whichever is nearer
(`--sub-record` takes the cut-outs and the board edge from another record, so a no-dummy board
is modelled on the same extent as the dummy board). "cell" and "col0" keep stage 2's rule (the
copper plus lambda0/2) so that they compare with stage 2's column.

  python openems/board_ant.py BOARD.json RECORD.json OUT.json --model bank --banks RX,TX
      [--loads 50|short|open] [--sub-record REC.json] [--plot PNG]
  python openems/board_ant.py BOARD.json RECORD.json OUT.json --model cell|col0 --col TX2
  python openems/board_ant.py ... --model bank --banks RX,TX --keep TXD0,TX1,TX2,RX3,RX4,RXD5
      # stage 3b E3: the isolation sub-model (only these columns; the board from their cells'
      # extent plus lambda0)

Needs shapely (and matplotlib for --plot).

cell: only the named column is kept; every other column's copper is removed (its gap outside the
cut-out becomes GND, its vias north of Pg stay; inside the cut-out the substrate is bare).
"""

import argparse
import json
import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

W50, GAP, LPORT, FOFF, ROW = 0.200, 0.200, 1.2, 0.50, 0.45
LAM0 = 299792458.0 / 62e9 * 1e3
D = 2.342


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


def periodic_span(inputs, cut):
    """x range covering every column's input period and the cut-out, in whole column pitches."""
    lo, hi = min(inputs) - D / 2, max(inputs) + D / 2
    lo -= math.ceil(max(0.0, lo - cut[0]) / D - 1e-9) * D
    hi += math.ceil(max(0.0, cut[2] - hi) / D - 1e-9) * D
    return lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("record")
    ap.add_argument("out")
    ap.add_argument("--model", required=True, choices=["cell", "bank", "col0"])
    ap.add_argument("--col", default="TX2")
    ap.add_argument("--banks", default="TX", help="bank model: TX, RX or RX,TX")
    ap.add_argument("--loads", default="50", choices=["50", "short", "open"])
    ap.add_argument("--sub-record", help="record whose cut-outs and board edge set the board")
    ap.add_argument("--keep", help="bank model: only these columns (comma-separated)")
    ap.add_argument("--plot")
    a = ap.parse_args()
    d = json.load(open(a.board))
    rec = json.load(open(a.record))
    srec = json.load(open(a.sub_record)) if a.sub_record else rec
    P = {k: v["at"] for k, v in rec["ports"].items() if "at" in v}
    cols = rec["columns"]
    lz = rec["params"]["dummy_load"]
    if a.model in ("cell", "col0"):
        banks = ["TX" if a.col.startswith("TX") else "RX"]
    else:
        banks = a.banks.split(",")
    cuts = {b: box(*rec["cutouts_u1_mm"][b]) for b in banks}
    CUT = unary_union(list(cuts.values()))
    bank_cols = [c for c in cols if c[:2] in banks]
    keep = [a.col] if a.model in ("cell", "col0") else bank_cols
    if a.keep and a.model == "bank":
        keep = [c for c in bank_cols if c in a.keep.split(",")]
    fed = [c for c in keep if not cols[c]["dummy"]]
    dummies = [c for c in keep if cols[c]["dummy"]]
    B = {b: rec["banks"][b] for b in banks}

    copper = {}
    for t in d["tracks"]:
        if t["layer"] == "F.Cu":
            copper.setdefault(t["net"], []).append(track_poly(t))
    for pd in d["pads"]:
        if (
            pd["layer"] == "F.Cu"
            and (pd["ref"].startswith("ANT_") or pd["ref"].startswith(("RT", "RL")))
            and pd["net"] != "GND"
        ):
            copper.setdefault(pd["net"], []).append(mp(pd["poly"]))
    copper = {k: unary_union(v) for k, v in copper.items()}
    load_pads = {}
    for pd in d["pads"]:
        if pd["layer"] == "F.Cu" and pd["ref"].startswith(("RT", "RL")):
            load_pads.setdefault(pd["ref"], {})[pd["net"]] = mp(pd["poly"])

    def bank_of(c):
        return c[:2]

    pname = "P1" if a.model == "col0" else "Pg"
    pg = {c: P[f"{c}.{pname}"] for c in fed}
    yport = {c: pg[c][1] - LPORT for c in fed}
    xin = {c: B[bank_of(c)]["inputs"][c] for c in keep}
    # dummy load cell: the via zone (x_in +- via_zone[0], Pg - via_zone[1] .. Pg)
    vzx, vzy = (float(v) for v in lz["via_zone"])
    cell_box = {
        c: box(xin[c] - vzx, B[bank_of(c)]["Pg"] - vzy, xin[c] + vzx, B[bank_of(c)]["Pg"])
        for c in dummies
    }

    # north / south parts of each bank's area (split between the banks midway along the strip)
    xs = sorted((rec["cutouts_u1_mm"][b][0], rec["cutouts_u1_mm"][b][2], b) for b in banks)
    splits = [0.5 * (xs[i][1] + xs[i + 1][0]) for i in range(len(xs) - 1)]
    area = {}
    for i, (_, _, b) in enumerate(xs):
        area[b] = (splits[i - 1] if i > 0 else -1e3, splits[i] if i < len(splits) else 1e3)
    north = unary_union([box(area[b][0], B[b]["Pg"], area[b][1], 1e3) for b in banks])
    south = unary_union([box(area[b][0], -1e3, area[b][1], B[b]["Pg"]) for b in banks])

    nets, leadgaps = {}, []
    for c in keep:
        g = copper[c].intersection(north)
        if c in fed:
            x, y = pg[c]
            if a.model == "col0":  # stage 2's column: the port lead below P1 only
                g = copper[c].intersection(box(-1e3, y, 1e3, 1e3))
            g = g.union(box(x - W50 / 2, yport[c], x + W50 / 2, y + 0.01))
            leadgaps.append(box(x - W50 / 2 - GAP, yport[c] - GAP, x + W50 / 2 + GAP, y + 0.001))
        else:
            g = g.union(copper[c].intersection(cell_box[c]))
        nets[c] = g
    cb = unary_union(list(nets.values())).bounds
    if a.model == "bank":
        sc = [srec["cutouts_u1_mm"][b] for b in banks]
        top = srec["board_frame"]["board_height_min"] - srec["board_frame"]["u1_at"][1]
        x_lo, x_hi = min(c[0] for c in sc) - LAM0, max(c[2] for c in sc) + LAM0
        if a.keep:  # the kept columns' cells plus the cut-out margin and lambda0
            kb = unary_union(list(nets.values())).bounds
            kx = [kb[0], kb[2]]
            x_lo = max(x_lo, min(kx) - 1.0 - LAM0)
            x_hi = min(x_hi, max(kx) + 1.0 + LAM0)
        SUB = box(
            x_lo,
            min(c[1] for c in sc) - LAM0,
            x_hi,
            min(max(c[3] for c in sc) + LAM0, top),
        )
    else:
        m_sub = 0.5 * LAM0
        SUB = box(cb[0] - m_sub, cb[1] - m_sub, cb[2] + m_sub, cb[3] + m_sub)
    sb = SUB.bounds

    # ---- L1 GND ------------------------------------------------------------------------------
    gnd_b = unary_union(
        [mp(f["poly"]) for f in d["fill"] if f["layer"] == "F.Cu" and f["net"] == "GND"]
    )
    keep_gap = unary_union([v.buffer(GAP) for v in nets.values()])
    removed = [copper[n] for n in copper if n[:2] in banks and n not in keep and n in cols]
    refill = unary_union([q.buffer(GAP + 0.01) for q in removed]) if removed else Polygon()
    refill = refill.difference(CUT).difference(keep_gap)
    gnd_n = gnd_b.union(refill).intersection(north)
    # beyond the macro's region (west and east, up to its top) the board's own L1 GND is assumed
    # to continue: plain pour with a 0.6 mm via grid (stage 3 owns that copper)
    rg = rec["region_u1_mm"]
    beyond = box(-1e3, -1e3, 1e3, rg["y"][1]).difference(box(rg["x"][0], -1e3, rg["x"][1], 1e3))
    beyond = beyond.intersection(north).intersection(SUB) if a.model == "bank" else Polygon()
    gnd_n = gnd_n.union(beyond)
    cells = unary_union(list(cell_box.values())) if cell_box else Polygon()
    lg = unary_union(leadgaps) if leadgaps else Polygon()
    gnd_s = south.intersection(SUB).difference(cells).difference(lg)
    gnd_s = gnd_s.union(gnd_b.intersection(cells))
    for c in dummies:  # the GND land joins the pour
        for ref, pads in load_pads.items():
            if c in pads:
                gnd_s = gnd_s.union(pads["GND"])
    gnd = gnd_n.union(gnd_s).buffer(0).intersection(SUB)
    gnd = unary_union([q for q in parts(gnd) if q.area > 1e-4])
    if a.model == "col0":
        gnd = Polygon()

    # ---- vias --------------------------------------------------------------------------------
    vias, periods = [], {}
    if a.model != "col0":
        for v in d["vias"]:
            if len(v) > 4 and v[4] != "GND":  # the PA feed's vias: south of every bank's Pg
                continue
            pt = Point(v[0], v[1])
            if SUB.contains(pt) and (north.contains(pt) or cells.contains(pt)):
                vias.append([v[0], v[1], v[2], v[3]])
        synth = []
        for b in banks:
            Pg = B[b]["Pg"]
            lo, hi = periodic_span(list(B[b]["inputs"].values()), rec["cutouts_u1_mm"][b])
            periods[b] = (lo, hi)
            rows = []
            k = 0
            while Pg - 0.40 - ROW * k > sb[1] + 0.2:
                rows.append(Pg - 0.40 - ROW * k)
                k += 1
            xcols = []  # (x, rows) per via column
            for n, xj in B[b]["inputs"].items():
                for e in (-D / 2, D / 2):
                    xcols.append((xj + e, rows))
                if n not in keep:  # a removed column (cell model): plain lattice
                    xcols += [(xj - FOFF, rows), (xj + FOFF, rows), (xj, rows)]
                elif n in fed:
                    xcols += [(xj - FOFF, rows), (xj + FOFF, rows)]
                    xcols.append((xj, [y for y in rows if y <= Pg - LPORT - GAP - 0.46]))
                else:  # dummy: its load cell, then the lattice below it
                    low = [y for y in rows if y <= Pg - vzy - 0.30]
                    xcols += [(xj - FOFF, low), (xj + FOFF, low), (xj, low)]
            a0, a1 = max(area[b][0], sb[0]) + 0.2, min(area[b][1], sb[2]) - 0.2
            step = D / 4
            x = lo - step
            while x > a0:
                xcols.append((x, rows))
                x -= step
            x = hi + step
            while x < a1:
                xcols.append((x, rows))
                x += step
            for x, ys in xcols:
                if a0 < x < a1:
                    synth += [(round(x, 5), round(y, 5)) for y in ys]
        bb = beyond.bounds if not beyond.is_empty else None
        if bb:
            for i in range(int(math.floor(bb[0] / 0.6)), int(math.ceil(bb[2] / 0.6)) + 1):
                for j in range(int(math.floor(bb[1] / 0.6)), int(math.ceil(bb[3] / 0.6)) + 1):
                    q = Point(i * 0.6, j * 0.6)
                    if beyond.buffer(-0.2).contains(q):
                        synth.append((round(q.x, 5), round(q.y, 5)))
        seen = []
        for x, y in sorted(set(synth)):
            pt = Point(x, y)
            if lg.distance(pt) < 0.16 + 0.01 or cells.buffer(0.30).contains(pt):
                continue
            if any(abs(x - u) < 0.30 and abs(y - w) < 0.30 for u, w, *_ in vias + seen):
                continue
            seen.append([x, y, 0.32, 0.15])
        vias += seen
        gnd = gnd.union(
            unary_union(
                [Point(v[0], v[1]).buffer(v[2] / 2, resolution=8) for v in vias]
            ).intersection(gnd.buffer(0.2))
        )

    # ---- L2 windows: holes of the In1.Cu fill under the kept patches ---------------------------
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
                assert abs(hp.area - (bx[2] - bx[0]) * (bx[3] - bx[1])) < 1e-3, ("window", bx)
                windows.append([round(v, 5) for v in bx])
    assert len(windows) == 2 * len(keep), (len(windows), keep)

    loads, shorts, load_mesh = [], [], []
    for ref, pads in sorted(load_pads.items()):
        sig = [n for n in pads if n != "GND"][0]
        if sig in keep:
            cc = pads[sig].centroid
            at = [round(cc.x, 5), round(cc.y, 5)]
            load_mesh.append(dict(ref=ref, at=at, half=0.10))
            if a.loads == "50":
                loads.append(dict(ref=ref, net=sig, at=at, half=0.10, R=50.0))
            elif a.loads == "short":
                shorts.append([at[0], at[1], 0.20])

    model = dict(
        model=a.model,
        board=a.board.split("/")[-1],
        col=a.col if a.model in ("cell", "col0") else None,
        banks=banks,
        columns=keep,
        fed=fed,
        loads_mode=a.loads,
        sub=[round(v, 5) for v in sb],
        cbox=[
            round(cb[0] - 0.2, 5),
            round(cb[1] - 0.2, 5),
            round(cb[2] + 0.2, 5),
            round(cb[3] + 0.2, 5),
        ],
        cutouts={b: rec["cutouts_u1_mm"][b] for b in banks},
        # the periodic x mesh is for comparing columns of a bank; a single cell keeps the old mesh
        periods=(
            {b: [round(v, 5) for v in periods[b]] + [D] for b in periods}
            if a.model == "bank"
            else {}
        ),
        gnd=holefree(gnd),
        nets={k: holefree(v) for k, v in nets.items()},
        vias=[[round(v[0], 5), round(v[1], 5), v[3]] for v in vias],
        shorts=shorts,
        windows=windows,
        loads=loads,
        load_mesh=load_mesh,
        ports=[dict(name=f"{c}.{pname}", net=c, at=pg[c], start_y=yport[c]) for c in fed],
        phase_centres={c: cols[c]["phase_centre"] for c in keep},
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
        "windows",
        len(windows),
        "loads",
        a.loads,
        [ld["ref"] for ld in loads],
        "fed",
        fed,
    )
    if a.plot:
        plot(model, cols, a.plot)


def plot(model, cols, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    from matplotlib.patches import Polygon as MP

    s = model["sub"]
    fig, ax = plt.subplots(figsize=(11, 11 * (s[3] - s[1]) / (s[2] - s[0])), dpi=130)
    ax.set_facecolor("white")
    ax.add_patch(plt.Rectangle(s[:2], s[2] - s[0], s[3] - s[1], fc="#6b4e2e", ec="none"))
    for q in model["gnd"]:
        ax.add_patch(MP(q, fc="#e8c547", ec="none"))
    for w in model["windows"]:
        ax.add_patch(
            plt.Rectangle((w[0], w[1]), w[2] - w[0], w[3] - w[1], fc="none", ec="c", ls=":", lw=0.8)
        )
    for k, v in model["nets"].items():
        for q in v:
            ax.add_patch(MP(q, fc=("#aaaaaa" if cols[k]["dummy"] else "#e0412f"), ec="none"))
    for v in model["vias"]:
        ax.add_patch(Circle(v[:2], 0.16, fc="k"))
    for ld in model["loads"]:
        ax.plot(*ld["at"], "c*", ms=8)
    for sh in model["shorts"]:
        ax.plot(*sh[:2], "mx", ms=8)
    for pt in model["ports"]:
        ax.plot(*pt["at"], "w+", ms=10)
        ax.plot([pt["at"][0]] * 2, [pt["start_y"], pt["at"][1]], "w-", lw=0.6)
        ax.annotate(
            pt["name"], pt["at"], color="w", fontsize=6, xytext=(3, -8), textcoords="offset points"
        )
    for _, (lo, hi, dd) in model["periods"].items():
        k = 0
        while lo + k * dd <= hi + 1e-6:
            ax.axvline(lo + k * dd, color="b", lw=0.3, ls="--")
            k += 1
    cbx = model["cbox"]
    ax.add_patch(
        plt.Rectangle(cbx[:2], cbx[2] - cbx[0], cbx[3] - cbx[1], fc="none", ec="m", ls="--", lw=0.6)
    )
    ax.set_xlim(s[0] - 0.2, s[2] + 0.2)
    ax.set_ylim(s[1] - 0.2, s[3] + 0.2)
    ax.set_aspect("equal")
    ax.set_title(
        f"{model['model']} {model['col'] or ','.join(model['banks'])} ({model['board']}, loads "
        f"{model['loads_mode']}), U1 frame mm; magenta: fine-mesh box; blue dashed: column "
        "periods; cyan dotted: L2 windows; star: 50 ohm load",
        fontsize=7,
    )
    plt.savefig(path, bbox_inches="tight")


if __name__ == "__main__":
    main()
