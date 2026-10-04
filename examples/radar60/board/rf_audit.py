"""RF uniformity audit of a filled board that carries the radar60 RF macro (A1-A5).

Run with KiCad's Python (it needs `pcbnew`) on a board whose zones are filled, for example the
board `python -m rfmacro drc --keep-filled` saves, or the integrated board after
`kicad-cli pcb drc --refill-zones --save-board`:

    <kicad python> rf_audit.py BOARD.kicad_pcb RECORD.json [--u1 X,Y] [--net-prefix RF_]
                               [--out audit.json]

RECORD.json is the macro's record (`generated/rfm1-*/rfm1-*.json`). The macro is rebuilt from
the record's parameters (rfmacro, standard library) for the reference geometry, and the board's
own copper is checked against the same rules as the generator's G1-G4, because the fill can
differ from the generator's polygons (min_thickness, islands, clearances):

- A1 entry contact and band: each feed and dummy run-in net's copper outside its cut-out, grown by
  the 0.20 mm gap, meets the cut-out once, over 0.600 mm (+-0.015 at the 10 um raster); the vias
  within guard_band of a cut-out are the macro's lattice sites;
- A2 congruence: the filled L1 GND and the L1 copper of every active column's window (column
  frame, mirrored for TX) equal RX1's (XOR <= 1e-3 mm²), and so do the vias in it;
- A3 unstitched GND: every filled L1 GND point of the RF region outside the package lies within
  stitch_reach (geodesic) of a GND via;
- A4 fence continuity: consecutive fence vias along every feed side <= 0.50 mm (G4 on the board's
  vias);
- A5 identity: the board's macro vias and dummy loads are the record's (positions within 1 um, a
  50 ohm load with two GND vias per dummy) and the rebuilt macro's hash is the record's.

`--u1` is U1's centre in KiCad coordinates (default 100,100: the macro board); `--net-prefix` is
the prefix the integration gives the macro's nets. Exit status 1 when any audit fails.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "rf"))

from rfmacro import macro as M  # noqa: E402
from rfmacro import report as R  # noqa: E402
from rfmacro import rules as G  # noqa: E402
from rfmacro.raster import Grid  # noqa: E402
from rfmacro.vias import Placer  # noqa: E402

MM = 1e6


def _polys(ps, tf):
    """(outline, holes) pairs of a SHAPE_POLY_SET in the U1 frame."""
    out = []
    for i in range(ps.OutlineCount()):
        o = ps.Outline(i)
        ring = [tf(o.CPoint(j).x / MM, o.CPoint(j).y / MM) for j in range(o.PointCount())]
        holes = []
        for h in range(ps.HoleCount(i)):
            hh = ps.Hole(i, h)
            holes.append(
                [tf(hh.CPoint(j).x / MM, hh.CPoint(j).y / MM) for j in range(hh.PointCount())]
            )
        out.append((ring, holes))
    return out


def extract(path, u1, prefix):
    b = pcbnew.LoadBoard(path)
    err = pcbnew.FromMM(0.002)

    def tf(x, y):
        return (x - u1[0], u1[1] - y)

    def net(name):
        return name[len(prefix) :] if prefix and name.startswith(prefix) else name

    gnd, copper, vias, loads = [], {}, [], []
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != "GND":
            continue
        if z.IsOnLayer(pcbnew.F_Cu):
            gnd += _polys(z.GetFilledPolysList(pcbnew.F_Cu), tf)
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            vias.append((tf(p.x / MM, p.y / MM), t.GetNetname()))
            continue
        if t.GetLayer() != pcbnew.F_Cu:
            continue
        ps = pcbnew.SHAPE_POLY_SET()
        t.TransformShapeToPolygon(ps, pcbnew.F_Cu, 0, err, pcbnew.ERROR_INSIDE)
        copper.setdefault(net(t.GetNetname()), []).extend(_polys(ps, tf))
    for fp in b.GetFootprints():
        val = fp.GetValue()
        for pad in fp.Pads():
            if not pad.IsOnLayer(pcbnew.F_Cu) or pad.GetNetname() == "GND":
                continue
            ps = pcbnew.SHAPE_POLY_SET()
            pad.TransformShapeToPolygon(ps, pcbnew.F_Cu, 0, err, pcbnew.ERROR_INSIDE)
            copper.setdefault(net(pad.GetNetname()), []).extend(_polys(ps, tf))
        if "50R" in val:
            nets = sorted(net(p.GetNetname()) for p in fp.Pads())
            c = fp.GetPosition()
            loads.append(dict(ref=fp.GetReference(), at=tf(c.x / MM, c.y / MM), nets=nets))
    return dict(gnd=gnd, copper=copper, vias=vias, loads=loads)


def fill(g, m, polys, tf=lambda q: q):
    for ring, holes in polys:
        a = g.empty()
        g.polygon(a, [tf(q) for q in ring])
        for h in holes:
            hm = g.empty()
            g.polygon(hm, [tf(q) for q in h])
            a = g.andnot(a, hm)
        for j in range(g.ny):
            m[j] |= a[j]


def a1(mc, ru, bd):
    """Entry contact and band on the board's copper."""
    out, ok = {}, True
    lines = list(mc.feeds) + list(mc.runins)
    for n in lines:
        bank = "RX" if n.startswith("RX") else "TX"
        c = mc.cutouts[bank]
        x0, y0 = c[0] - 1.5, c[1] - 3.0
        g = Grid(x0, y0, c[2] + 1.5, c[3] + 0.5, 0.01)
        cu = g.empty()
        fill(g, cu, bd["copper"].get(n, []))
        cut = g.empty()
        g.rect(cut, *c)
        outside = g.andnot(cu, cut)
        # the gap: 0.20 mm round the copper (+ half a pixel: the disk's first row inside the
        # cut-out would otherwise lose a pixel each side)
        halo = g.andnot(g.dilate(outside, 0.20 / g.h + 0.5), outside)
        contact = g.and_(halo, cut)
        pieces = [g.describe(p) for p in g.components(contact) if len(p) > 2]
        spans = [p["bbox"][2] - p["bbox"][0] + g.h for p in pieces]
        good = len(pieces) == 1 and abs(spans[0] - 2 * ru.lchan) <= 0.015
        ok = ok and good
        out[n] = dict(contacts=len(pieces), span_mm=[round(s_, 3) for s_ in spans], ok=good)
    lattice = {(round(v[0][0], 3), round(v[0][1], 3)) for v in mc.vias if v[3] in ("runin", "ring")}
    foreign = []
    for q, _ in bd["vias"]:
        if any(G.rect_dist(q, c) < ru.B - 1e-6 for c in mc.cutouts.values()):
            if (round(q[0], 3), round(q[1], 3)) not in lattice:
                foreign.append([round(q[0], 3), round(q[1], 3)])
    ok = ok and not foreign
    return dict(lines=out, foreign_vias_in_band=foreign, ok=ok)


def a2(mc, ru, bd):
    """Column congruence of the filled board."""
    wb = G.window_bounds(ru)
    act = [n for n, c in mc.columns.items() if not c.dummy]
    scenes = {}
    for n in act:
        tf, _ = G.column_frame(mc.columns[n])
        g = Grid(wb[0], wb[1], wb[2], wb[3], 0.01)
        cu = g.empty()
        for polys in bd["copper"].values():
            fill(g, cu, polys, tf)
        gnd = g.empty()
        fill(g, gnd, bd["gnd"], tf)
        vs = sorted(
            (round(tf(q)[1], 4), round(tf(q)[0], 4))
            for q, _ in bd["vias"]
            if wb[0] <= tf(q)[0] <= wb[2] and wb[1] <= tf(q)[1] <= wb[3]
        )
        scenes[n] = (g, cu, gnd, vs)
    ref = scenes[act[0]]
    res, worst = {}, [0.0, 0.0, 0]
    for n in act[1:]:
        g, cu, gnd, vs = scenes[n]
        a_cu = g.area(g.xor(cu, ref[1]))
        a_g = g.area(g.xor(gnd, ref[2]))
        vd = G._via_diff(vs, ref[3])
        res[n] = dict(copper_xor_mm2=round(a_cu, 5), gnd_xor_mm2=round(a_g, 5), via_mismatch=vd)
        worst = [max(worst[0], a_cu), max(worst[1], a_g), max(worst[2], vd)]
    ok = worst[0] <= 1e-3 and worst[1] <= 1e-3 and worst[2] == 0
    return dict(reference=act[0], columns=res, ok=ok)


def a3(mc, ru, bd):
    """Unstitched GND of the filled board."""
    x0, x1 = mc.region["x"]
    y0, y1 = mc.region["y"]
    g = Grid(x0, y0, x1, y1, 0.01)
    gnd = g.empty()
    fill(g, gnd, bd["gnd"])
    h = ru.half
    body = g.empty()
    g.rect(body, -h, -h, h, h)
    gnd = g.andnot(gnd, body)
    seeds = g.empty()
    g.points(seeds, [q for q, n in bd["vias"] if n.endswith("GND")])
    got = g.geodesic_reach(seeds, gnd, float(mc.params["stitch_reach"]) + G.REACH_TOL, 3)
    pieces = [g.describe(p) for p in g.components(g.andnot(gnd, got)) if len(p) >= 4]
    return dict(gnd_area_mm2=round(g.area(gnd), 3), unreached=pieces, ok=not pieces)


def a4(mc, ru, bd):
    """Fence continuity with the board's vias."""
    pl = Placer(mc, ru)
    for q, _ in bd["vias"]:
        pl.idx.add(q)
    seqs = G.fence_sequences(mc, ru, pl)
    rows = {
        k: dict(max_spacing_mm=round(v["worst"], 3), over=len(v["bad"]), openings=len(v["holes"]))
        for k, v in seqs.items()
    }
    ok = all(v["over"] == 0 and v["openings"] == 0 for v in rows.values())
    return dict(limit_mm=0.50, rows=rows, ok=ok)


def a5(mc, rec, bd):
    """The board carries the record's macro."""
    from rfmacro.geom import PointIndex

    ix = PointIndex(0.5)
    for q, _ in bd["vias"]:
        ix.add(q)
    missing = [list(v[0]) for v in mc.vias if ix.nearest(v[0], 0.001)[1] < 0]
    loads = {ld["ref"]: ld for ld in bd["loads"]}
    bad_loads = []
    for n, ld in mc.loads.items():
        b = loads.get(ld.ref)
        nets_ok = b is not None and sorted([n, "GND"]) == b["nets"]
        vias_ok = all(ix.nearest(q, 0.001)[1] >= 0 for q in ld.vias)
        if not (nets_ok and vias_ok and math.dist(b["at"], ld.centre) < 0.001):
            bad_loads.append(ld.ref)
    sha = R.record(mc)["geometry_sha256"]
    ok = not missing and not bad_loads and sha == rec["geometry_sha256"]
    return dict(
        macro_vias_missing=len(missing),
        loads_found=len(bd["loads"]),
        loads_bad=bad_loads,
        geometry_sha256_matches=sha == rec["geometry_sha256"],
        ok=ok,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("board")
    ap.add_argument("record")
    ap.add_argument("--u1", default="100,100", help="U1 centre in KiCad coordinates (mm)")
    ap.add_argument("--net-prefix", default="")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    with open(a.record) as fh:
        rec = json.load(fh)
    u1 = tuple(float(v) for v in a.u1.split(","))
    bd = extract(a.board, u1, a.net_prefix)
    mc = M.build(rec["params"])
    ru = M.Rules(mc.params, mc.dims)
    res = dict(
        board=os.path.basename(a.board),
        A1_entry_and_band=a1(mc, ru, bd),
        A2_congruence=a2(mc, ru, bd),
        A3_unstitched_gnd=a3(mc, ru, bd),
        A4_fence_continuity=a4(mc, ru, bd),
        A5_identity=a5(mc, rec, bd),
    )
    res["ok"] = all(v["ok"] for k, v in res.items() if k.startswith("A"))
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
    summary = {k: v["ok"] for k, v in res.items() if k.startswith("A")}
    print(json.dumps(dict(board=res["board"], ok=res["ok"], audits=summary), indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
