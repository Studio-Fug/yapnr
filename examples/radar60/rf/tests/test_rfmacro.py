"""Tests of the radar60 RF macro generator and its RF-uniformity checks (standard library).

Run from examples/radar60/rf:  python3 -m unittest discover -s tests -v
The macro build takes about half a minute; it is built once for the whole module.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "board")
)

import rf_audit as RA  # noqa: E402
from rfmacro import coupons  # noqa: E402
from rfmacro import dims as dims_mod  # noqa: E402
from rfmacro import macro as M  # noqa: E402
from rfmacro import rules as G  # noqa: E402
from rfmacro.geom import Path, Seg, path_samples, rect  # noqa: E402
from rfmacro.params import resolve  # noqa: E402
from rfmacro.raster import Grid  # noqa: E402
from rfmacro.vias import Placer, _plan, fence_row  # noqa: E402

MC = None


def macro():
    global MC
    if MC is None:
        MC = M.build({"variant": 0})
    return MC


def check(mc, prefix):
    return next(c for c in mc.checks if c["check"].startswith(prefix))


class RasterTest(unittest.TestCase):
    def test_reach_is_calibrated(self):
        """3 steps at 0.46 mm reach every pixel centre within 0.452 mm and none beyond 0.459 mm
        (probed here at 0.445 / 0.470 mm: the probe point snaps to the nearest pixel)."""
        g = Grid(0, 0, 2, 2, 0.01)
        seeds = g.empty()
        g.points(seeds, [(1.0, 1.0)])
        got = g.geodesic_reach(seeds, g.full(), 0.46, 3)
        for k in range(36):
            a = 2 * math.pi * k / 36
            for r_, want in ((0.445, True), (0.470, False)):
                q = (1.0 + r_ * math.cos(a), 1.0 + r_ * math.sin(a))
                i = int(round((q[0] - g.x0) / g.h - g.ph))
                j = int(round((q[1] - g.y0) / g.h - g.ph))
                self.assertEqual(bool((got[j] >> i) & 1), want, (r_, k))

    def test_reach_does_not_cross_a_gap(self):
        g = Grid(0, 0, 2, 1, 0.01)
        dom = g.full()
        gap = g.empty()
        g.rect(gap, 1.0, 0.0, 1.2, 1.0)  # a 0.2 mm GCPW gap
        dom = g.andnot(dom, gap)
        seeds = g.empty()
        g.points(seeds, [(0.9, 0.5)])
        got = g.geodesic_reach(seeds, dom, 0.46, 3)
        right = g.empty()
        g.rect(right, 1.2, 0.0, 2.0, 1.0)
        self.assertEqual(g.count(g.and_(got, right)), 0)


def toy_macro(vias):
    """Two parallel lines 1.0 mm apart, joined into a U at the top: the GND tongue between
    them is a sliver unless a via sits in it."""
    p = Path((0.0, 0.0), math.pi / 2, 0.2)
    p.straight(3.0).turn(0.5, -180).straight(3.0)
    params = dict(stitch_reach=0.45)
    mc = SimpleNamespace(
        pour=[rect(-0.9, 0.0, 1.9, 3.6), rect(-0.9, 0.0, -0.8, 0.1)],
        cutouts={},
        antipads=[],
        unstitched=[],
        load_channels=[],
        feeds={"A": p},
        runins={},
        vias=[(v, 0.15, 0.32, "fence") for v in vias],
        region=dict(x=[-0.9, 1.9], y=[0.0, 3.6]),
        params=params,
    )
    ru = SimpleNamespace(lchan=0.3, half=0.0)
    return mc, ru


class StitchTest(unittest.TestCase):
    def outer(self):
        """The two outer fence rows (0.5 mm outside the legs, 0.45 mm pitch)."""
        out = []
        for y in [0.2 + 0.45 * k for k in range(8)]:
            out += [(-0.5, y), (1.5, y)]
        return out

    def test_unstitched_tongue_is_flagged(self):
        mc, ru = toy_macro(self.outer())
        res = G.stitch_raster(mc, ru)
        tongue = [pc for pc in res["pieces"] if 0.3 < pc["at"][0] < 0.7 and pc["long_mm"] > 1.0]
        self.assertTrue(tongue, res["pieces"])

    def test_tongue_with_its_row_is_stitched(self):
        vias = self.outer() + [(0.5, 3.0 - 0.45 * k) for k in range(7)]
        mc, ru = toy_macro(vias)
        res = G.stitch_raster(mc, ru)
        tongue = [pc for pc in res["pieces"] if 0.2 < pc["at"][0] < 0.8]
        self.assertEqual(tongue, [])


class PlanTest(unittest.TestCase):
    def test_plan_spacing_between_fixed_vias(self):
        pos = _plan(0.0, 3.0, True, True, 0.24, 0.45, 0.48, 0.43)
        pts = [-0.24] + pos + [3.24]
        gaps = [b - a for a, b in zip(pts, pts[1:])]
        self.assertTrue(all(0.43 - 1e-9 <= g <= 0.48 + 1e-9 for g in gaps), gaps)


class MacroTest(unittest.TestCase):
    def test_all_checks_pass(self):
        mc = macro()
        bad = [c["check"] for c in mc.checks if c.get("ok") is False]
        self.assertEqual(bad, [])

    def test_equal_lengths_and_dummies(self):
        mc = macro()
        for bank in (("RX1", "RX2", "RX3", "RX4"), ("TX1", "TX2", "TX3")):
            ls = [mc.feeds[n].length - mc.feeds[n].marks["P0"][1] for n in bank]
            self.assertLess(max(ls) - min(ls), 1e-6)
        self.assertEqual(sorted(mc.loads), ["RXD0", "RXD5", "TXD0", "TXD4"])
        self.assertEqual(sum(1 for c in mc.columns.values() if c.dummy), 4)

    def test_entry_contact_is_the_runin_only(self):
        g1 = check(macro(), "G1")
        for n, r in g1["lines"].items():
            self.assertAlmostEqual(r["contact_span_mm"], 0.6, delta=0.005, msg=n)
            self.assertGreaterEqual(r["band_min_mm"], 0.85 - 1e-6, n)
        self.assertEqual(g1["foreign_vias_in_band"], [])

    def test_columns_congruent(self):
        w = check(macro(), "G2")["worst"]
        self.assertLessEqual(w["copper_xor_mm2"], 1e-3)
        self.assertLessEqual(w["gnd_xor_mm2"], 1e-3)
        self.assertEqual(w["via_mismatch"], 0)

    def test_no_unstitched_gnd_and_fences(self):
        mc = macro()
        self.assertEqual(check(mc, "G3")["unreached_pieces"], [])
        g4 = check(mc, "G4")
        self.assertTrue(all(r["max_spacing_mm"] <= 0.5 for r in g4["rows"].values()))

    def test_d12_variants_share_the_layout(self):
        g6 = check(macro(), "G6")
        self.assertEqual(len(set(g6["layout_sha256_per_variant"].values())), 1)

    def test_vias_keep_the_fab_spacing(self):
        mc = macro()
        from rfmacro.geom import PointIndex

        ix = PointIndex(0.5)
        for v in mc.vias:
            for j in ix.within(v[0], 0.43 - 1e-6):
                self.fail(f"vias {v[0]} and {ix.pts[j]} closer than 0.43 mm")
            ix.add(v[0])

    def test_only_the_lattice_above_pg(self):
        """Every via between Pg and the cut-out is a run-in pair or a ring site."""
        mc = macro()
        ru = M.Rules(mc.params, mc.dims)
        for v in mc.vias:
            for c in mc.cutouts.values():
                if G.rect_dist(v[0], c) < ru.lout - 1e-6:
                    self.assertIn(v[3], ("runin", "ring"), v)

    def test_shared_row_between_close_lines(self):
        mc = macro()
        ru = M.Rules(mc.params, mc.dims)
        pl = Placer(mc, ru)
        f = mc.feeds["TX3"]  # TX2 runs 1.3 mm north of TX3's first leg: one row midway
        row = fence_row(f, 1, 2.0, 6.0, pl, "TX3")
        ys = {round(q[1], 3) for _, q in row if q is not None}
        self.assertEqual(ys, {0.65})


class LoadAndWindowTest(unittest.TestCase):
    def test_load_cells_identical_and_no_via_at_a_land(self):
        g7 = check(macro(), "G7")
        self.assertTrue(g7["ok"], g7)
        for r in g7["loads"].values():
            self.assertEqual(r["vias_in_zone"], 5)
            self.assertEqual(r["vias_at_lands"], [])

    def test_a_via_in_a_gnd_land_fails_g7(self):
        mc = macro()
        ld = mc.loads["TXD0"]
        cx = sum(q[0] for q in ld.pad2) / len(ld.pad2)
        cy = sum(q[1] for q in ld.pad2) / len(ld.pad2)
        ru = M.Rules(mc.params, mc.dims)
        bad = SimpleNamespace(**vars(mc))
        bad.vias = list(mc.vias) + [((cx, cy), 0.15, 0.32, "fill")]
        g7 = G.load_cells(bad, ru)
        self.assertFalse(g7["ok"])
        self.assertTrue(g7["loads"]["TXD0"]["vias_at_lands"])

    def test_g2_window_is_symmetric_and_the_open_ends_declared(self):
        g2 = check(macro(), "G2")
        w = g2["window_column_frame"]
        self.assertAlmostEqual(w[0], -w[2], places=6)
        self.assertGreater(w[2], 1.5 * 2.342 + 0.1)  # holds the second-neighbour input lines
        opened = sorted(n for n, r in g2["columns"].items() if r["open_end"])
        self.assertEqual(opened, ["RX1", "TX3"])
        for n in opened:
            self.assertGreater(g2["columns"][n]["declared_difference"]["copper_xor_mm2"], 0.5)

    def test_rects_minus(self):
        r = M.rects_minus([(0, 0, 10, 10)], [(2, 2, 4, 4), (9, 4, 11, 6)])
        self.assertAlmostEqual(sum((a[2] - a[0]) * (a[3] - a[1]) for a in r), 100 - 4 - 2)
        for a in r:
            self.assertFalse(a[0] < 3 < a[2] and a[1] < 3 < a[3])


def old_macro():
    """The pre-fix macro (fixture: rfm1-n of ab57757, filled by KiCad) in the new code's types.
    Pg is put 0.90 mm (runin_out) before each feed enters its cut-out."""
    here = os.path.dirname(os.path.abspath(__file__))
    with gzip.open(os.path.join(here, "fixtures", "old-rfm1-n.json.gz")) as fh:
        fx = json.load(fh)
    p = resolve({"variant": 0})
    d = dims_mod.compute(p)
    mc, ru = M.Macro(p, d), M.Rules(p, d)
    om = fx["macro"]
    mc.cutouts = {k: tuple(v) for k, v in om["cutouts"].items()}
    mc.pour, mc.region = om["pour"], om["region"]
    for n, f in om["feeds"].items():
        q = Path(tuple(f["start"]), f["heading"], f["width"])
        q.segs = [
            Seg(
                s["kind"],
                tuple(s["p0"]),
                tuple(s["p1"]),
                s["width"],
                tuple(s["center"]) if s["center"] else None,
                s["radius"],
                s["a0"],
                s["sweep"],
            )
            for s in f["segs"]
        ]
        q.marks = {k: (tuple(v[0]), v[1]) for k, v in f["marks"].items()}
        c = mc.cutouts[n[:2]]
        s_e = next(
            s
            for pt, _, s in path_samples(q, 0.005)
            if c[0] <= pt[0] <= c[2] and c[1] <= pt[1] <= c[3]
        )
        pg = next(pt for pt, _, s in path_samples(q, 0.005) if s >= s_e - ru.lout)
        q.marks["E"], q.marks["Pg"] = (None, s_e), (pg, s_e - ru.lout)
        mc.feeds[n] = q
    for n, c in om["columns"].items():
        col = M.build_column(f"COL_{n}", n, tuple(c["origin"]), c["mirror"], p, d)
        col.dummy = False
        mc.columns[n] = col
    bd = fx["board"]
    bd["vias"] = [(tuple(q), n) for q, n in bd["vias"]]
    return mc, ru, bd


class OldGeometryAuditTest(unittest.TestCase):
    """Review 2026-10-04: the board audit must fail on the geometry of the owner's finding."""

    @classmethod
    def setUpClass(cls):
        cls.mc, cls.ru, cls.bd = old_macro()

    def test_a1_finds_the_encroachments(self):
        r = RA.a1(self.mc, self.ru, self.bd)
        self.assertFalse(r["ok"])
        lines = r["lines"]
        for n in ("RX2", "RX3"):  # the bumps along the cut-out's south edge
            self.assertAlmostEqual(lines[n]["span_mm"][0], 0.88, delta=0.03, msg=n)
        self.assertAlmostEqual(lines["TX2"]["span_mm"][0], 1.49, delta=0.03)  # serpentine top
        self.assertEqual(lines["TX1"]["contacts"], 2)  # finger beside the package corner
        self.assertAlmostEqual(max(lines["TX1"]["span_mm"]), 0.96, delta=0.05)
        for n in ("RX1", "RX4", "TX3"):
            self.assertTrue(lines[n]["ok"], n)

    def test_a2_a3_a4_fail(self):
        self.assertFalse(RA.a2(self.mc, self.ru, self.bd)["ok"])
        a3 = RA.a3(self.mc, self.ru, self.bd)
        self.assertFalse(a3["ok"])
        self.assertGreater(max(p["area_mm2"] for p in a3["unreached"]), 1.0)
        self.assertFalse(RA.a4(self.mc, self.ru, self.bd)["ok"])

    def test_contact_span_follows_the_boundary(self):
        """A contact along the cut-out's side edge reads its length, not its x extent."""
        g = Grid(0, 0, 4, 4, 0.01)
        contact = g.empty()
        g.rect(contact, 1.0, 1.0, 1.02, 2.0)  # 1.0 mm along the west edge of the cut-out
        spans = RA.contact_spans(g, contact, (1.0, 0.5, 3.0, 3.0))
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0], 1.0, delta=0.02)


class CouponTest(unittest.TestCase):
    def test_new_coupons(self):
        st = coupons.build_strip()
        ids = {c["id"] for c in st.catalog}
        for want in ("CP-T", "CP-T2", "CP-T3", "CP-A", "CP-A-CORP", "CP-Z", "_G3"):
            self.assertIn(want, ids)
        self.assertEqual(len(st.loads), 3)
        t = {c["id"]: c for c in st.catalog}
        self.assertAlmostEqual(t["CP-T"]["p0_to_p1_mm"], t["CP-T3"]["p0_to_p1_mm"], places=6)
        self.assertAlmostEqual(t["CP-T2"]["p0_to_p1_mm"], t["CP-T3"]["p0_to_p1_mm"], places=6)
        # the strip is stitched like the macro: what no via reaches is only small corners
        self.assertLess(sum(m["area_mm2"] for m in t["_G3"]["made_gap"]), 0.5)
        # every coupon load is the macro's load cell: its five vias, nothing else in its zone
        for ld in st.loads:
            z = ld.via_zone
            inside = [v for v in st.vias if z[0] <= v[0][0] <= z[2] and z[1] <= v[0][1] <= z[3]]
            self.assertEqual(len(inside), 5, ld.ref)


if __name__ == "__main__":
    unittest.main()
