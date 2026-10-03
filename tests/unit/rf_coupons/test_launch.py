"""The Cinch 142-0701-851 edge launch of the Order 0 boards (launch.py, Order 0 design §5): the
connector drawing, the shipped 2D designs of regions M and W and their rules, and the KiCad
geometry the board writer draws from them. The 2D re-solve needs the FEA environment (scikit-fem,
gmsh) and skips without it."""

from __future__ import annotations

import math
import re
import unittest

from yapnr.rf.coupons import catalog, launch, layout

try:
    import gmsh  # noqa: F401
    import skfem  # noqa: F401

    HAVE_FEA = True
except ImportError:
    HAVE_FEA = False

C851 = launch.CINCH_142_0701_851


class ConnectorTest(unittest.TestCase):
    def test_drawing_values(self):
        """The product drawing and the end-launch table, in mm."""
        self.assertAlmostEqual(C851.tab_w, 0.508, places=3)
        self.assertAlmostEqual(C851.tab_t, 0.254, places=3)
        self.assertAlmostEqual(C851.tab_len, 1.905, places=3)
        self.assertAlmostEqual(C851.body_w, 9.525, places=3)
        self.assertAlmostEqual(C851.leg_len, 4.75, places=2)
        self.assertEqual(tuple(round(v, 3) for v in C851.gnd_pad_y), (3.175, 5.588))
        self.assertAlmostEqual(C851.lay_e, 5.08, places=3)
        # the slot takes the 1.6 mm board: 1.73 mm against OSH Park's 1.51 mm of laminate+copper
        bd = launch.OSH_FR408HR
        board = 2 * (bd.t_out + bd.h_pp + bd.t_in) + bd.h_core
        self.assertGreater(C851.slot, board)
        self.assertLess(C851.slot - board, 0.25)

    def test_legs_land_on_the_ground_pads(self):
        lo, hi = C851.leg_y
        c, d = C851.gnd_pad_y
        self.assertTrue(c < lo < hi < d)
        self.assertLess(C851.leg_len, C851.lay_e)
        # Cinch's plated holes sit next to the leg's centre line
        self.assertAlmostEqual(C851.pth_y, (lo + hi) / 2, delta=0.1)


class DesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = launch.load()

    def test_regions_are_rule_clean(self):
        for reg, d in self.d.items():
            self.assertEqual(launch.check(d, launch.STICK_W[reg] / 2), [], reg)

    def test_ten_mm_sticks_are_too_narrow(self):
        """Design §16 (R16): the -851's ground pads need 12 mm M sticks."""
        probs = launch.check(self.d["M"], 5.0)
        self.assertTrue(any("ground pads" in p for p in probs), probs)

    def test_tab_aware_pad(self):
        for reg, d in self.d.items():
            self.assertGreaterEqual(d.pad_w - C851.tab_w, 0.2, reg)  # fillet room both sides
            self.assertGreaterEqual(d.x_pe - d.x_tab, 0.3, reg)  # toe
            self.assertAlmostEqual(d.x0, launch.OSH_FR408HR.keepback)
            # the tab lowers the pad's impedance: the bare toe needs a narrower gap
            self.assertLess(d.gap_bare, d.gap_tab, reg)
            self.assertGreaterEqual(min(d.gap_tab, d.gap_bare), launch.OSH_FR408HR.min_space)
            s = d.sensitivity
            self.assertLess(abs(s["fillet 1"] - s["fillet 0"]), 3.0, reg)

    def test_m_transition(self):
        d = self.d["M"]
        self.assertTrue(0.38 <= d.line_w <= 0.42)
        self.assertTrue(1.0 <= d.x_te - d.x_pe <= 1.5)
        self.assertGreaterEqual(d.cut_pad, d.pad_w / 2 + d.gap_tab + launch.CUT_MARGIN - 1e-6)
        cut = d.cut_profile()
        self.assertEqual(cut[0], (0.0, d.cut_pad))
        self.assertEqual(cut[-1][1], 0.0)
        self.assertTrue(all(b[1] <= a[1] + 1e-9 for a, b in zip(cut, cut[1:])))
        for s in d.taper:
            self.assertAlmostEqual(s["z0"], 50.0, delta=1.0)
        # halfway between stations, where the layout interpolates: still near 50 ohm
        self.assertEqual(len(d.taper_mid), len(d.taper) - 1)
        for m in d.taper_mid:
            self.assertAlmostEqual(m["z0"], 50.0, delta=2.0)
        # the flare ends at the M keep-away, near 50 ohm
        self.assertAlmostEqual(d.flare[-1]["gap"], launch.M_KEEPAWAY)
        self.assertAlmostEqual(d.flare[-1]["z0"], d.line["z0"], delta=0.5)

    def test_w_transition(self):
        d = self.d["W"]
        self.assertEqual(d.cut_profile(), [])
        self.assertEqual(d.cut_layers, ())
        self.assertEqual(d.taper[-1]["w"], d.line_w)
        ws = [s["w"] for s in d.taper]
        self.assertEqual(ws, sorted(ws))
        for s in list(d.taper[:-1]) + list(d.taper_mid):
            self.assertAlmostEqual(s["z0"], 50.0, delta=1.0)
        # the ground ends at the W keep-away: the 3 mm line is below 50 ohm even without it
        end = d.channel(x_to=d.x_end + 5.0)[-1][1]
        self.assertAlmostEqual(end, d.line_w / 2 + launch.W_KEEPAWAY)
        self.assertLess(d.line["z0"], 50.0)

    def test_channel_and_outline(self):
        for reg, d in self.d.items():
            prof = d.channel()
            self.assertEqual(prof[0][0], 0.0)
            xs = [x for x, _ in prof]
            self.assertEqual(xs, sorted(xs))
            for x, hw in prof:
                if x <= C851.lay_e:
                    self.assertLessEqual(hw, C851.gnd_pad_y[0] + 1e-9, (reg, x))
            out = d.signal_outline()
            self.assertGreaterEqual(min(x for x, _ in out), launch.OSH_FR408HR.keepback)
            for x, y in out:  # the copper sits inside the channel
                self.assertLess(abs(y), d.channel_at(x) + 1e-9, (reg, x))

    def test_vias(self):
        for reg, d in self.d.items():
            half = launch.STICK_W[reg] / 2
            vias = d.vias(half)
            for i, a in enumerate(vias):
                self.assertGreaterEqual(a.x - launch.VIA_D / 2, launch.OSH_FR408HR.keepback - 1e-9)
                self.assertLessEqual(abs(a.y) + launch.VIA_D / 2, half - 0.381 + 1e-9)
                self.assertGreater(abs(a.y) - launch.VIA_D / 2, d.channel_at(a.x), (reg, a))
                for b in vias[i + 1 :]:
                    self.assertGreaterEqual(math.hypot(a.x - b.x, a.y - b.y), 0.8 - 1e-9)
            for sgn in (-1, 1):
                legs = [v for v in vias if v.role == "leg" and v.y * sgn > 0]
                self.assertGreaterEqual(len(legs), 3, reg)

    def test_fence_stops_mid_stick(self):
        d = self.d["M"]
        self.assertLessEqual(max(v.x for v in d.vias(6.0, 9.55)), 9.55)


class BoardTest(unittest.TestCase):
    def test_check_boards(self):
        for reg in ("M", "W"):
            b = catalog.launch_check_board(reg)
            self.assertEqual(b.launch.design, f"oshpark-4l-fr408hr:{reg}")
            text, panel = layout.board_text(b.stackup, b)
            self.assertEqual(text.count('(footprint "SMA_EdgeLaunch_Cinch_142-0701-851"'), 4)
            self.assertNotIn("JLCJLCJLCJLC", text)
            vias = re.findall(r"\(via \(at [^)]*\) \(size ([\d.]+)\) \(drill ([\d.]+)\)", text)
            self.assertTrue(vias)
            self.assertEqual({v for v in vias}, {("0.55", "0.3")})
            inner = re.findall(r'\(zone \(layers ((?:"In\d\.Cu" ?)+)\) \(uuid', text)
            if reg == "M":
                self.assertEqual(sum('"In1.Cu"' in z and "In2" not in z for z in inner), 4)
            else:
                self.assertEqual(sum('"In1.Cu" "In2.Cu"' in z for z in inner), 2)
            self.assertEqual(len(panel.mask_rules), 2)

    def test_osh_rules(self):
        pro = layout.project_json("x", layout.RULES_OSHPARK_4L)
        rules = pro["board"]["design_settings"]["rules"]
        self.assertEqual(rules["min_copper_edge_clearance"], 0.381)
        self.assertEqual(
            layout.project_json("x")["board"]["design_settings"]["rules"][
                "min_copper_edge_clearance"
            ],
            0.25,
        )


@unittest.skipUnless(HAVE_FEA, "scikit-fem and gmsh not installed (FEA environment only)")
class ResolveTest(unittest.TestCase):
    """The shipped M numbers against a direct 2D solve."""

    def test_m_pad_and_line(self):
        d = launch.load()["M"]
        bd = launch.OSH_FR408HR
        z, _ = launch.impedance(
            bd, "M", d.pad_w, d.gap_tab, cut=d.cut_pad, tab=C851, fillet=0.5, mask_gap=True
        )
        self.assertAlmostEqual(z, 50.0, delta=0.3)
        z, _ = launch.impedance(bd, "M", d.line_w, None)
        self.assertAlmostEqual(z, d.line["z0"], delta=0.1)


if __name__ == "__main__":
    unittest.main()
