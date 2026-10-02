"""Stackups, the stick catalogue and the panel layout (design §4, §5, §11.3)."""

from __future__ import annotations

import json
import math
import re
import unittest

import numpy as np

from yapnr.rf.coupons import catalog, fab, jsonfmt, layout, stackups

A = "JLC04161H-7628"
B = "JLC06161H-7628"


class StackupTest(unittest.TestCase):
    def test_thickness_and_layers(self):
        for sid, n_cu in ((A, 4), (B, 6), ("JLC06161H-2116C", 6)):
            st = stackups.get(sid)
            self.assertEqual(len(st.copper), n_cu)
            self.assertAlmostEqual(st.thickness_mm, 1.6, delta=0.16)  # JLC: ±10 %
            self.assertEqual(st.kicad_layer(1), "F.Cu")
            self.assertEqual(st.kicad_layer(n_cu), "B.Cu")
            self.assertEqual(st.kicad_layer(2), "In1.Cu")
            for n in st.params:
                p = stackups.PARAMS[n]
                self.assertGreater(p.sigma, 0)
                self.assertTrue(p.lo <= p.nominal <= p.hi, n)
                if p.table:
                    self.assertTrue(p.table[0] < p.nominal < p.table[1], n)

    def test_shared_l1_cross_section(self):
        """Board B's L1 dielectric is board A's (design §4.2): the tie set measures it."""
        a, b = stackups.get(A).layers, stackups.get(B).layers
        self.assertEqual((a[1].material, a[1].t_mm), (b[1].material, b[1].t_mm))

    def test_overlay_layers(self):
        st = stackups.get(B)
        layers = stackups.physical_layers(st, {"core.h": 0.41, "pp.dk": 4.3, "L3.t": 0.017})
        core = [x for x in layers if x["name"] == "core"][0]
        self.assertEqual(core["t_mm"], 0.41)
        self.assertEqual([x for x in layers if x["name"] == "pp1"][0]["er"], 4.3)
        self.assertEqual([x for x in layers if x["name"] == "In2.Cu"][0]["t_mm"], 0.017)


class CatalogTest(unittest.TestCase):
    def test_conditioning_table(self):
        """Design §5.1: worst best-pair |sin β ΔL| of the TRL set over 1-6 / 1-12 GHz."""
        for eps in (2.90, 3.19, 3.41, 4.48):
            lo = catalog.conditioning(catalog.TRL_DL, eps, np.linspace(1e9, 6e9, 2001)).min()
            hi = catalog.conditioning(catalog.TRL_DL, eps, np.linspace(1e9, 12e9, 2001)).min()
            self.assertGreater(lo, 0.974, eps)
            self.assertGreater(hi, 0.95, eps)
        tie = catalog.conditioning(catalog.TIE_DL, 3.19, np.linspace(1e9, 6e9, 2001)).min()
        self.assertAlmostEqual(tie, 0.93, delta=0.01)

    def test_sticks(self):
        for sid, n_core in ((A, 22), (B, 19)):
            b = catalog.board(sid)
            ids = [s.id for s in b.sticks]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(sum(1 for s in b.sticks if s.tier == "core"), n_core)
            for set_id, t in b.trl.items():
                lens = sorted(b.stick(x).dl for x in t["sticks"])
                self.assertEqual(lens, sorted(t["dl"]), set_id)
                self.assertEqual(b.stick(t["reflect"]).kind, "reflect")
            for s in b.sticks:
                if s.kind in ("thru", "line", "verify"):
                    self.assertAlmostEqual(s.length, 2 * catalog.LAUNCH_MM + s.dl)

    def test_tuned_resonators(self):
        """Stub, ring and coupler dimensions are set at nominal so their features sit at
        5.8 GHz (the ring's first notch at 2.9 GHz)."""
        b = fab.tuned_board(A)
        stub = b.stick("A15").geometry["stub_mm"]
        self.assertAlmostEqual(
            stub, 7.24, delta=0.3
        )  # design §5.2: 7.24 mm less the end correction
        self.assertAlmostEqual(b.stick("A14").geometry["radius"], 8.906, delta=0.1)
        self.assertAlmostEqual(b.stick("A17").geometry["length"], 7.06, delta=0.15)
        rb = fab.tuned_board(B).stick("B11").geometry["radius"]
        self.assertAlmostEqual(rb, 7.774, delta=0.1)

    def test_catalog_json(self):
        doc = fab.tuned_board(A).to_json()
        text = jsonfmt.dumps(doc)
        self.assertEqual(json.loads(text)["schema"], "yapnr-coupon-catalog/1")


class PanelTest(unittest.TestCase):
    def _check(self, sid):
        b = fab.tuned_board(sid)
        sticks = [s for s in b.sticks if s.generated]
        panel = layout.pack(sticks)
        boxes = []
        for pl in panel.placed:
            s = pl.stick
            x0, x1 = pl.x0, pl.x0 + s.length
            y0, y1 = pl.yc - s.height / 2, pl.yc + s.height / 2
            self.assertGreaterEqual(x0, layout.ORIGIN[0] + layout.RAIL + layout.SLOT - 1e-9)
            self.assertLessEqual(
                x1, layout.ORIGIN[0] + panel.width - layout.RAIL - layout.SLOT + 1e-9
            )
            self.assertLessEqual(
                y1, layout.ORIGIN[1] + panel.height - layout.RAIL - layout.SLOT + 1e-9
            )
            for a in boxes:  # no overlap, at least a slot apart
                sep = max(a[0] - x1, x0 - a[1], a[2] - y1, y0 - a[3])
                self.assertGreaterEqual(sep, layout.SLOT - 1e-9)
            boxes.append((x0, x1, y0, y1))
            original = next(x for x in sticks if x.id == s.id)
            self.assertGreaterEqual(s.height, original.height)
        self.assertEqual(len(panel.placed), len(sticks))
        return panel

    def test_board_a(self):
        p = self._check(A)
        self.assertLess(p.width * p.height, 190.0 * 200.0)

    def test_board_b(self):
        p = self._check(B)
        self.assertLess(p.width * p.height, 150.0 * 200.0)

    def test_board_text(self):
        for sid in (A, B):
            text, panel = layout.board_text(sid, fab.tuned_board(sid))
            depth = 0
            for ch in text:
                depth += (ch == "(") - (ch == ")")
                self.assertGreaterEqual(depth, 0)
            self.assertEqual(depth, 0)
            self.assertNotRegex(text, r"[ \t]+\n")
            self.assertIn('(layer "dielectric 1" (type "prepreg") (thickness 0.2104)', text)
            nets = re.findall(r'\(net \d+ "([^"]*)"\)', text)
            self.assertEqual(len(nets), len(set(nets)))
            self.assertIn("(arc ", text)  # the ring
            for name in panel.mask_rules:
                self.assertIn(f"memberOfFootprint('{name}')", layout.dru_text(panel.mask_rules))


class LabelTest(unittest.TestCase):
    def test_reference_plane_ticks(self):
        st = stackups.get(A)
        wr = layout.Writer(st, catalog.board(A))
        s = catalog.board(A).stick("A05")
        wr.labels(layout.Placed(s, 0.0, 0.0))
        ticks = [g for g in wr.items.graphics if "gr_line" in g]
        self.assertEqual(len(ticks), 4)
        self.assertTrue(math.isclose(s.rp[1] - s.rp[0], s.dl))


if __name__ == "__main__":
    unittest.main()
