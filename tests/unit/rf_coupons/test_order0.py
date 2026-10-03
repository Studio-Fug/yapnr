"""Board O, the OSH Park 4-layer Order 0 uploads (Order 0 design §3-§6): stackups and priors,
line families, the catalogue, the frameless panel, the KiCad text, the tag's QR and the expected
S-parameters."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import tempfile
import unittest

import numpy as np

from yapnr.rf.coupons import (
    catalog,
    expected,
    fab,
    families,
    launch,
    layout,
    layout_o,
    models,
    qr,
    stackups,
    touchstone,
)

FR = "OSHPARK-4L-FR408HR"
EM = "OSHPARK-4L-EM528"
URL = "https://github.com/Studio-Fug/yapnr/tree/main/docs/rf/order0"


def _have_tables() -> bool:
    try:
        t = families.load(FR)
    except FileNotFoundError:
        return False
    return all(f in t for f in families.BOARD_FAMILIES[FR])


class StackupTest(unittest.TestCase):
    def test_osh_priors(self):
        """OSH Park's published values (design §3.1, §9 priors)."""
        fr, em = stackups.get(FR), stackups.get(EM)
        self.assertEqual((fr.board, em.board), ("O", "O"))
        p = fr.prior()
        self.assertEqual((p["pp.dk"].nominal, p["pp.dk"].sigma), (3.61, 0.10))
        self.assertEqual(p["core.dk"].nominal, 3.87)
        self.assertAlmostEqual(p["pp1.h"].sigma * 2 / 0.0254, 0.797, places=2)  # ±0.797 mil
        self.assertAlmostEqual(p["mask.scale"].sigma, 1 / 3, places=2)  # 0.6 ± 0.2 mil
        self.assertEqual(em.prior()["pp.dk"].nominal, 3.76)
        self.assertEqual(em.prior()["core.dk"].nominal, 4.11)
        # the JLC priors are untouched
        self.assertEqual(stackups.param("pp.dk").nominal, 4.4)

    def test_em528_inside_the_fr408hr_tables(self):
        """EM528 reads FR408HR's tables: its nominal point lies inside their ranges."""
        self.assertTrue(families.table_path(EM).endswith(f"{FR}.json"))
        fr = stackups.params_of(stackups.get(FR))
        for n, p in stackups.get(EM).prior().items():
            if fr[n].table:
                lo, hi = fr[n].table
                self.assertTrue(lo < p.nominal < hi, n)

    def test_board_b_is_the_product_stackup(self):
        st = stackups.get("JLC06161H-2116C")
        self.assertEqual(st.board, "B")
        self.assertEqual(st.prior()["pp1.h"].nominal, 0.2464)
        b = catalog.board("JLC06161H-2116C")
        self.assertEqual(b.launch.pad_gap, 0.435)  # 2D, L2 and L3 cut, L4 reference
        self.assertIn("B24", [s.id for s in b.sticks])  # the P-MO tie variant
        self.assertNotIn("B24", [s.id for s in catalog.board("JLC06161H-7628").sticks])


class FamilyTest(unittest.TestCase):
    def test_board_o_families(self):
        f = families.of(FR)
        self.assertEqual(f["M"].w, 0.40)  # 4 pixels of the 0.10 mm grid
        self.assertEqual(f["W"].w, 3.0)  # 6 pixels of the 0.50 mm grid
        self.assertEqual((f["M0.7"].w, f["M1.4"].w), (0.28, 0.56))
        self.assertTrue(f["M-MK"].mask and not f["M"].mask)
        self.assertEqual(f["W"].params, families.DEEP)
        self.assertEqual(set(f["W"].regions), {"pp", "core"})
        self.assertEqual(families.get("M", "JLC04161H-7628").w, 0.348)  # board A's M
        self.assertEqual(families.get("S", "JLC06161H-2116C").w, 0.279)

    def test_w_dispersion_substrate(self):
        v = stackups.with_values(stackups.get(FR), {})
        h, er = families.get("W", FR).dispersion_substrate(v)
        self.assertAlmostEqual(h, 2 * 0.1999 + 0.9906, places=4)
        self.assertTrue(3.61 < er < 3.87)

    @unittest.skipUnless(_have_tables(), "board O tables not built")
    def test_tables_at_nominal(self):
        """The shipped surrogates: M near 50 Ω, the masked line 1-2 Ω lower (design §4.1)."""
        m = models.Model(FR, stackups.with_values(stackups.get(FR), {}), np.array([5e9]))
        z = {f: float(m.line(f).zc[0].real) for f in ("M", "M0.7", "M1.4", "M-MK", "W")}
        self.assertAlmostEqual(z["M"], 50.3, delta=0.7)
        self.assertTrue(59.0 < z["M0.7"] < 63.0 and 39.5 < z["M1.4"] < 43.0)
        self.assertTrue(0.8 < z["M"] - z["M-MK"] < 2.5)
        self.assertAlmostEqual(z["W"], 48.6, delta=0.8)
        eps = float(m.line("M").eps_eff[0])
        self.assertAlmostEqual(eps, 2.685, delta=0.03)


class CatalogTest(unittest.TestCase):
    def test_uploads(self):
        m = catalog.board(FR, "M")
        ids = [s.id for s in m.sticks]
        for sid in (
            "A01",
            "A05",
            "A06",
            "A07",
            "A10",
            "A11",
            "A12",
            "A14",
            "A15",
            "A16",
            "A04R",
            "R1",
        ):
            self.assertIn(sid, ids)
        self.assertNotIn("D1", ids)  # D1 moved to O0-D (D-O0-10)
        for s in m.sticks:
            if s.kind in ("thru", "line", "verify", "reflect", "variant", "switch"):
                self.assertEqual(s.height, 12.0, s.id)  # review: 12 mm M sticks
        self.assertEqual(m.stick("A04R").geometry["rot"], 90)
        self.assertEqual(sorted(m.trl["M"]["dl"]), [0.0, 4.5, 13.0, 30.0, 70.0])
        w = catalog.board(FR, "W")
        self.assertEqual(
            [s.id for s in w.sticks if s.trl == "W" and s.kind != "demo"][:5],
            ["B01", "B02", "B03", "B04", "B05"],
        )
        self.assertTrue(all(s.height == 16.0 for s in w.sticks if s.kind in ("thru", "line")))
        self.assertEqual(w.stick("D2").kind, "window")
        d = catalog.board(FR, "D")
        self.assertEqual(sorted(s.id for s in d.sticks), ["A01", "A04", "D1", "R1"])

    def test_conditioning(self):
        """Design §4.2/§4.3: the M set >= 0.926 over 0.5-6 GHz for εeff 2.60-2.95, the W set
        >= 0.929 over 1-6 GHz for 2.85-3.25."""
        for eps in (2.60, 2.75, 2.95):
            c = catalog.conditioning(catalog.O_TRL_M, eps, np.linspace(0.5e9, 6e9, 2001))
            self.assertGreater(c.min(), 0.925, eps)
        for eps in (2.85, 3.05, 3.25):
            c = catalog.conditioning(catalog.O_TRL_W, eps, np.linspace(1e9, 6e9, 2001))
            self.assertGreater(c.min(), 0.928, eps)

    def test_references(self):
        """R1 / R1t on the optimizer's grid: the λ/4 35.36 Ω arm to the junction centre."""
        for reg, (w_arm, lc, pitch) in (("M", (0.70, 8.9, 0.1)), ("W", (5.0, 8.5, 0.5))):
            r = catalog.o_reference(reg)
            (ax0, ax1, ay0, ay1), (ox0, ox1, _, _) = r["copper"]
            self.assertAlmostEqual(ay1 - ay0, w_arm)
            self.assertAlmostEqual((ox0 + ox1) / 2, lc)
            for v in (ax1, ay1 - ay0, ox1 - ox0, r["h"]):
                self.assertAlmostEqual(v / pitch, round(v / pitch), places=6)

    def test_demo_geometry(self):
        """Each demo port: the standard launch half (10 mm to its RP) and L_f to the window."""
        for upload, sid in (("M", "R1"), ("W", "R1t"), ("W", "D2"), ("D", "D1")):
            s = catalog.board(FR, upload).stick(sid)
            g = s.geometry
            win = g["window"]
            self.assertAlmostEqual(win["x0"], catalog.LAUNCH_MM + win["feed"])
            self.assertAlmostEqual(s.height, win["h"] + 2 * (catalog.LAUNCH_MM + win["feed"]))
            self.assertEqual([p["side"] for p in g["ports"]], ["W", "N", "S"])

    def test_tuned_resonators(self):
        b = fab.tuned_board(FR, "M") if _have_tables() else None
        if b is None:
            self.skipTest("board O tables not built")
        r = b.stick("A11").geometry["radius"]
        self.assertAlmostEqual(r, 15.1, delta=0.3)  # design: n = 3 at 5.79 GHz
        self.assertAlmostEqual(b.stick("A12").geometry["stub_mm"], 7.81, delta=0.3)


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panels = {u: layout_o.pack(catalog.board(FR, u).sticks) for u in "MWD"}

    def test_size_and_cost(self):
        """Within the design's estimates (§16.7) at $10 per square inch."""
        limits = {"M": 26.0, "W": 12.6, "D": 6.6}
        for u, p in self.panels.items():
            self.assertLess(p.sq_in, limits[u], u)

    def test_one_outline_and_slots(self):
        for u, p in self.panels.items():
            loops = layout_o.outline_loops(p)
            boxes = [
                (
                    min(x for x, _ in lp),
                    min(y for _, y in lp),
                    max(x for x, _ in lp),
                    max(y for _, y in lp),
                )
                for lp in loops
            ]
            outer = [
                b
                for b in boxes
                if not any(
                    c != b and c[0] <= b[0] and c[1] <= b[1] and b[2] <= c[2] and b[3] <= c[3]
                    for c in boxes
                )
            ]
            self.assertEqual(len(outer), 1, u)  # frameless: one outline, the rest cut-outs
            its = p.items
            for i, a in enumerate(its):
                for b in its[i + 1 :]:
                    gap = max(
                        b.x - (a.x + a.w), a.x - (b.x + b.w), b.y - (a.y + a.h), a.y - (b.y + b.h)
                    )
                    self.assertGreaterEqual(gap, layout_o.SLOT - 1e-6, (u, a.stick.id, b.stick.id))

    def test_tabs_away_from_launches(self):
        """Tabs on allowed edges only: never on a 2-port stick's short (launch) ends, and at
        least TAB_CLEAR from them; three bites on each side of every tab."""
        for u, p in self.panels.items():
            self.assertEqual(len(p.bites), 6 * len(p.tabs), u)
            for x0, x1, y0, y1 in p.tabs:
                for it in p.items:
                    if it.stick.kind in ("demo", "window") or it.stick.ports != 2:
                        continue
                    if it.rot == 90:
                        lo, hi, a0, a1 = it.y, it.y + it.h, y0, y1
                        touch = abs(x1 - it.x) < 1e-6 or abs(x0 - (it.x + it.w)) < 1e-6
                    else:
                        lo, hi, a0, a1 = it.x, it.x + it.w, x0, x1
                        touch = abs(y1 - it.y) < 1e-6 or abs(y0 - (it.y + it.h)) < 1e-6
                    if touch and a1 > lo and a0 < hi:
                        self.assertGreaterEqual(a0 - lo, layout_o.TAB_CLEAR - 1e-6, it.stick.id)
                        self.assertGreaterEqual(hi - a1, layout_o.TAB_CLEAR - 1e-6, it.stick.id)

    def test_board_text(self):
        b = catalog.board(FR, "M")
        text, panel = layout.board_text(FR, b, "A", dict(title="t", git="g"))
        depth = 0
        for ch in text:
            depth += (ch == "(") - (ch == ")")
            self.assertGreaterEqual(depth, 0)
        self.assertEqual(depth, 0)
        self.assertNotRegex(text, r"[ \t]+\n")
        nets = re.findall(r'\(net \d+ "([^"]*)"\)', text)
        self.assertEqual(len(nets), len(set(nets)))
        self.assertIn('(color "Purple") (thickness 0.0152)', text)
        self.assertIn("(epsilon_r 3.61)", text)
        self.assertIn('"Reference_R1"', text)
        self.assertIn('"R_0402_1005Metric"', text)
        self.assertEqual(
            text.count('(footprint "SMA_EdgeLaunch_Cinch_142-0701-851"'), 33
        )  # design §16.7: 33 SMAs per O0-M copy
        dru = layout.dru_text(panel.mask_rules, bites=True)
        self.assertIn("memberOfFootprint('MB1')", dru)
        self.assertIn("physical_hole_clearance", dru)


class QrTest(unittest.TestCase):
    def test_reed_solomon_and_format(self):
        # the "HELLO WORLD" 1-M example (thonky.com QR tutorial)
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.rs_ecc(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])
        self.assertEqual(format(qr._format_bits(0), "015b"), "101010000010010")

    def test_tag_url(self):
        """Version 4-M (33 x 33) as the design says; the module grid matches python-qrcode's
        (checked once, 2026-10-02, for all eight masks)."""
        m = qr.matrix(URL)
        self.assertEqual(len(m), 33)
        bits = "".join("".join("1" if v else "0" for v in r) for r in m)
        self.assertEqual(
            hashlib.sha256(bits.encode()).hexdigest(),
            "0b4b4c4aa0faef56e50533aabbd3b4ca25adf943e0ad3cdb200998b3cb068064",
        )
        self.assertEqual(qr.capacity(4), 62)
        with self.assertRaises(ValueError):
            qr.matrix("x" * 200)


@unittest.skipUnless(_have_tables(), "board O tables not built")
class ExpectedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_both_substrates(self):
        paths = expected.write_o("M", self.dir)
        for sub in (FR, EM):
            self.assertIn(f"{sub}:A04", paths)
            self.assertIn(f"{sub}:A16:2", paths)
        f, s_fr, _ = touchstone.read(paths[f"{FR}:A04"])
        _, s_em, _ = touchstone.read(paths[f"{EM}:A04"])
        k = int(np.argmin(abs(f - 5e9)))
        loss = -20 * math.log10(abs(s_fr[k, 1, 0]))
        self.assertTrue(0.3 < loss < 0.7, loss)  # 3 cm at 0.13-0.16 dB/cm (design §4.1)
        # EM528's higher εeff: more phase over the 30 mm line
        ph_fr = np.unwrap(np.angle(s_fr[:, 1, 0]))[k]
        ph_em = np.unwrap(np.angle(s_em[:, 1, 0]))[k]
        self.assertLess(ph_em, ph_fr)
        # the C-pad is a capacitor at 30-300 MHz
        f1, s1, _ = touchstone.read(paths[f"{FR}:A16:1"])
        z = 50 * (1 + s1[:, 0, 0]) / (1 - s1[:, 0, 0])
        k1 = int(np.argmin(abs(f1 - 100e6)))
        c = -1 / (2 * math.pi * f1[k1] * z[k1].imag)
        c_pp = 8.854e-12 * 3.6 * 36e-6 / 0.1999e-3
        self.assertTrue(1.0 < c / c_pp < 1.4, c / c_pp)  # parallel plate plus fringing


class LaunchFrameTest(unittest.TestCase):
    def test_frames_map_launch_axes(self):
        """A launch on each edge: x into the stick, the footprint angle turns its x axis."""
        s = catalog.board(FR, "M").stick("R1")
        pl = layout.Placed(s, 0.0, 0.0)
        for side, at, into in (
            ("W", 0.0, (1, 0)),
            ("E", 0.0, (-1, 0)),
            ("N", 20.0, (0, 1)),
            ("S", 20.0, (0, -1)),
        ):
            fr = layout.Frame(pl, side, at)
            p0, p1 = fr.p(0.0, 0.0), fr.p(1.0, 0.0)
            self.assertAlmostEqual(p1[0] - p0[0], into[0])
            self.assertAlmostEqual(p1[1] - p0[1], into[1])
            a = math.radians(fr.angle)  # KiCad: local (1, 0) -> (cos a, -sin a) on screen
            self.assertAlmostEqual(math.cos(a), into[0])
            self.assertAlmostEqual(-math.sin(a), into[1])
        rot = layout.Placed(s, 10.0, 5.0, rot=90)
        self.assertEqual(rot.p(1.0, 0.0), (10.0, 6.0))
        self.assertEqual(rot.angle, -90.0)
        self.assertLess(launch.CINCH_142_0701_851.lay_e, layout_o.TAB_CLEAR)


if __name__ == "__main__":
    unittest.main()
