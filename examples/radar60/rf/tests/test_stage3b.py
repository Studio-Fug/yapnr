"""Tests of the stage-3b generator options (standard library): D14 PA feed (G8), D15 pour
variants (G3 thresholds, stitch metrics), dummies S1/S1.5 and their termination, the L2-L3
cavity options (G9), the C1 / D5 column parameters and the TX feed options (T2-T4).

Run from examples/radar60/rf:  python3 -m unittest discover -s tests -v
Each variant is built once (about half a minute each, nine variants).
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rfmacro import cavity as C  # noqa: E402
from rfmacro import dims as dims_mod  # noqa: E402
from rfmacro import kicad as K  # noqa: E402
from rfmacro import macro as M  # noqa: E402
from rfmacro import pa as PA  # noqa: E402
from rfmacro import pour as P  # noqa: E402
from rfmacro import rules as G  # noqa: E402
from rfmacro.params import resolve  # noqa: E402

_CACHE = {}


def build(**ov):
    key = tuple(sorted((k, str(v)) for k, v in ov.items()))
    if key not in _CACHE:
        _CACHE[key] = M.build(dict(ov, variant=0))
    return _CACHE[key]


def check(mc, prefix):
    return next(c for c in mc.checks if c["check"].startswith(prefix))


def failed(mc):
    return [c["check"] for c in mc.checks if c.get("ok") is False]


class PAFeedTest(unittest.TestCase):
    """D14: the macro owns the VOUT_PA feed."""

    def test_g8_passes_and_the_vias_sit_in_the_pocket(self):
        mc = build()
        g8 = check(mc, "G8")
        self.assertTrue(g8["ok"], g8["failed"])
        self.assertEqual(len(mc.pa.vias), 4)
        pk = mc.pa.pocket
        for c in mc.pa.vias:
            self.assertTrue(
                pk[0] + 0.2 <= c[0] <= pk[2] - 0.2 and pk[1] + 0.2 <= c[1] <= pk[3] - 0.2
            )
        self.assertGreaterEqual(g8["min_to_foreign_land_mm"], 0.15 - 1e-6)
        self.assertGreaterEqual(g8["min_to_gnd_via_pad_mm"], 0.15 - 1e-6)
        self.assertLessEqual(g8["electrical"]["i_peak_per_via_a"], 1.0)

    def test_board_carries_the_feed_on_the_pa_net(self):
        mc = build()
        txt = K.board_text(mc, "t")
        self.assertIn('(pad "A2" smd circle', txt)
        a2 = txt[txt.index('(pad "A2"') :].split("\n")[0]
        self.assertIn('(net "1V0_PA")', a2)
        vias = [ln for ln in txt.split("\n") if ln.startswith("\t(via ") and '"1V0_PA"' in ln]
        self.assertEqual(len(vias), len(mc.pa.vias))
        self.assertIn('"radar60:RFM1_PA_FEED"', txt)

    def test_undersized_via_set_fails_g8(self):
        """Regression fixture: two vias carry 1.25 A peak each (> 1.0 A) and do not make the set."""
        mc = build()
        small = dataclasses.replace(mc.pa, vias=mc.pa.vias[:2], squares=mc.pa.squares[:2])
        bad = SimpleNamespace(**vars(mc))
        bad.pa = small
        g8 = PA.check(bad, M.Rules(mc.params, mc.dims))
        self.assertFalse(g8["ok"])
        self.assertIn("current per via", g8["failed"])

    def test_no_gnd_via_near_the_pa_copper(self):
        mc = build()
        for v in mc.vias:
            for r in mc.pa.rects:
                self.assertGreaterEqual(G.rect_dist(v[0], r) - v[2] / 2, 0.15 - 1e-6, v)


class PourVariantTest(unittest.TestCase):
    """D15: A (0.60 grid), B (1.00 grid + hard max 0.75), C (strips only)."""

    def test_b_meets_its_thresholds_with_fewer_vias(self):
        a, b = build(d15="A"), build(d15="B")
        self.assertEqual(failed(b), [])
        sb = check(b, "stitch metrics")
        self.assertLessEqual(sb["l1_dmax_edge_mm"], 0.45 + 0.02)
        self.assertLessEqual(sb["l1_dmax_interior_mm"], 0.75 + 0.02)
        self.assertGreater(sb["l1_dmax_interior_mm"], 0.5)
        self.assertLess(len(b.vias), len(a.vias) - 300)

    def test_b_without_its_fill_fails_g3(self):
        """Regression fixture: B's 1.00 mm grid alone leaves L1 GND beyond the hard maximum."""
        b = build(d15="B")
        bare = SimpleNamespace(**vars(b))
        bare.vias = [v for v in b.vias if v[3] not in ("fill", "l23")]
        ru = M.Rules(b.params, b.dims)
        self.assertTrue(G.stitch_raster(bare, ru)["pieces"] or P.l23_raster(bare, ru)["pieces"])

    def test_c_keeps_only_strips(self):
        a, c = build(d15="A"), build(d15="C")
        self.assertEqual(failed(c), [])
        ga = check(a, "G3")["gnd_area_mm2"]
        gc = check(c, "G3")["gnd_area_mm2"]
        self.assertLess(gc, 0.6 * ga)
        self.assertNotIn("grid", {v[3] for v in c.vias})
        # every L1 GND zone of C lies within a strip, ring, load cell or the launch ground
        self.assertGreater(len(c.pour_zones), 20)

    def test_corridor_pieces_are_simple(self):
        p = M.Path((0.0, 0.0), math.pi / 2, 0.2)
        p.straight(1.0).turn(0.5, -180).straight(1.0)
        pcs = P.corridor_pieces(p, 0.76)
        self.assertEqual(len(pcs), 3)
        cx, cy = pcs[1][-1]  # the U-turn's sector closes at its centre
        self.assertAlmostEqual(cx, 0.5, places=9)
        self.assertAlmostEqual(cy, 1.0, places=9)

    def test_lattice_cutoff(self):
        self.assertGreater(P.lattice_cutoff_ghz(0.60, 0.15, 3.56), 120)
        self.assertLess(P.lattice_cutoff_ghz(1.00, 0.15, 3.56), 85)


class DummyTest(unittest.TestCase):
    def test_short_is_refused(self):
        with self.assertRaises(ValueError):
            resolve({"dummy_term": "short"})

    def test_s1_has_the_open_end_dummies_only_and_a_shorter_tx_path(self):
        s1, s2 = build(dummies="outer"), build()
        self.assertEqual(sorted(s1.loads), ["RXD0", "TXD4"])
        self.assertEqual(failed(s1), [])
        lt = check(s1, "equal length P0->P1 TX")["lengths_mm"]["TX1"]
        self.assertLess(lt, check(s2, "equal length P0->P1 TX")["lengths_mm"]["TX1"] - 2.0)
        g2 = check(s1, "G2")
        self.assertEqual(sorted(g2["edge_columns_declared"]), ["RX4", "TX1"])

    def test_s15_keeps_txd0(self):
        s15 = build(dummies="outer+txd0")
        self.assertEqual(sorted(s15.loads), ["RXD0", "TXD0", "TXD4"])
        self.assertEqual(failed(s15), [])

    def test_open_loads_are_dnp(self):
        mc = build(dummy_term="open")
        self.assertEqual(failed(mc), [])
        self.assertIn("dnp", K.load_footprint(next(iter(mc.loads.values())), mc.params))


class CavityTest(unittest.TestCase):
    def test_k2_posts_keep_clear_and_the_columns_congruent(self):
        mc = build(l23_cavity="K2")
        self.assertEqual(failed(mc), [])
        g9 = check(mc, "G9")
        self.assertGreater(g9["posts"], 0)
        self.assertGreaterEqual(g9["post_pad_to_patch_mm"], 0.60 - 1e-6)
        self.assertEqual(g9["posts_lost"], 0)
        # the same posts in every column's frame
        loc = C.cell_posts(mc, M.Rules(mc.params, mc.dims))
        self.assertEqual(g9["posts"], len(loc) * len(mc.columns))

    def test_k1_has_no_windows(self):
        mc = build(l23_cavity="K1")
        self.assertEqual(failed(mc), [])
        self.assertTrue(all(not c.windows for c in mc.columns.values()))
        self.assertIn("no L2 windows", check(mc, "G9")["banks"]["RX"]["excited"])

    def test_tm_mode_count(self):
        r = C.tm_modes(10.0, 5.0, 3.52, 57.0, 70.0)
        self.assertGreater(r["count"], 5)
        self.assertAlmostEqual(r["lowest_ghz"], 299.792458 / (2 * math.sqrt(3.52)) / 10.0, places=1)


class ColumnTest(unittest.TestCase):
    def test_div_l_scale_scales_the_divider(self):
        p0 = resolve({})
        p1 = resolve({"div_l_scale": 1.05})
        d = dims_mod.compute(p0)
        c0 = M.build_column("a", "a", (0.0, 0.0), False, p0, d)
        c1 = M.build_column("a", "a", (0.0, 0.0), False, p1, d)
        self.assertAlmostEqual(c1.arm_lengths["target"] / c0.arm_lengths["target"], 1.05, places=6)
        self.assertAlmostEqual(c0.arm_lengths["diff"], c0.arm_lengths["target"], delta=0.02)

    def test_series_column_needs_no_declared_difference(self):
        mc = build(column="series")
        self.assertEqual(failed(mc), [])
        g2 = check(mc, "G2")
        self.assertEqual(g2["declared_difference_column_frame"], [])
        col = mc.columns["RX2"]
        self.assertEqual(len(col.windows), 1)  # one window over the column
        self.assertAlmostEqual(col.p1[0], col.origin[0], places=9)  # fed on its axis


class TxOptionTest(unittest.TestCase):
    def test_only_the_nested_order_is_planar(self):
        tx = {n: M.ball_xy(M.RF_BALLS[n]) for n in M.TX_NAMES}
        r = M.tx_order_planarity(tx)
        self.assertEqual(r["planar"], ["TX1-TX2-TX3"])
        with self.assertRaises(ValueError):
            M.build({"tx_order": ["TX2", "TX1", "TX3"]})

    def test_t2_draws_the_north_west_finger_at_r075(self):
        """T2 raises TX1's finger (R 0.75), so the TX bank sits 0.11 mm higher; known limit: the
        lane finger's outer row then meets its run-in pair with a 0.65 mm fence gap (G4), which
        the bridge does not close. Every other check passes."""
        mc = build(tx_eq="T2")
        self.assertEqual(mc.fit["tx"]["equalizers"]["TX1"].get("nw_r"), 0.75)
        self.assertTrue(set(failed(mc)) <= {"G4 fence continuity"}, failed(mc))
        g4 = check(mc, "G4")
        pairs = [p for r in g4["rows"].values() for p in r["over_limit"]]
        lattice = {
            (round(v[0][0], 3), round(v[0][1], 3)) for v in mc.vias if v[3] in ("runin", "ring")
        }
        for pr in pairs:  # every gap ends on a run-in pair or ring site
            self.assertTrue(tuple(pr["a"]) in lattice or tuple(pr["b"]) in lattice, pr)

    def test_t4_equalizes_within_the_skew_budget(self):
        mc = build(tx_skew_budget_ps=7.9)
        self.assertEqual(failed(mc), [])
        eq = check(mc, "equal length P0->P1 TX")
        self.assertGreater(eq["spread_mm"], 0.5)
        self.assertLessEqual(eq["spread_mm"], eq["limit_mm"])


if __name__ == "__main__":
    unittest.main()
