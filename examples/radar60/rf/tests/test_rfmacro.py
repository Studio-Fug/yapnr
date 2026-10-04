"""Tests of the radar60 RF macro generator and its RF-uniformity checks (standard library).

Run from examples/radar60/rf:  python3 -m unittest discover -s tests -v
The macro build takes about half a minute; it is built once for the whole module.
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rfmacro import coupons  # noqa: E402
from rfmacro import macro as M  # noqa: E402
from rfmacro import rules as G  # noqa: E402
from rfmacro.geom import Path, rect  # noqa: E402
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


class CouponTest(unittest.TestCase):
    def test_new_coupons(self):
        st = coupons.build_strip()
        ids = {c["id"] for c in st.catalog}
        for want in ("CP-T", "CP-T3", "CP-A", "CP-A-CORP", "CP-Z"):
            self.assertIn(want, ids)
        self.assertEqual(len(st.loads), 3)
        t = {c["id"]: c for c in st.catalog}
        self.assertAlmostEqual(t["CP-T"]["p0_to_p1_mm"], t["CP-T3"]["p0_to_p1_mm"], places=6)


if __name__ == "__main__":
    unittest.main()
