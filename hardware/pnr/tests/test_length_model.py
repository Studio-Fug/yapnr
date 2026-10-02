"""KiCad-equivalent routed length (pnr.length_model): stackup, lines, pads, vias.

The expected numbers are KiCad 10.0.6 DRC measurements (``length`` rule) of the same
geometry, taken with pnr.length_oracle on ladder boards; the oracle test below repeats
some of them when kicad-cli is available.
"""

import math
import unittest

from pnr import length_model as lm

SGPS = """
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" power)
    (6 "In2.Cu" power)
    (2 "B.Cu" signal)
  )
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "FR4")
        (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4")
        (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "FR4")
        (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (copper_finish "None")
      (dielectric_constraints no)
    )
  )
"""


def square_pad(net, centre, size=1.0, layer="F.Cu"):
    return lm.PadCopper(
        net, centre, frozenset((layer,)), lm.rounded_rect(centre, (size, size), 0.0, 0.0)
    )


class StackupTest(unittest.TestCase):
    def test_default_two_layer_via_is_kicads(self):
        st = lm.default_stackup(2)
        self.assertAlmostEqual(lm.layer_distance(st, "F.Cu", "B.Cu"), 1.58, places=9)

    def test_board_stackup_spans(self):
        st = lm.read_stackup(SGPS)
        self.assertEqual(st["planes"], ["In1.Cu", "In2.Cu"])
        # KiCad 10.0.6: full outer copper, half inner copper, everything between.
        for a, b, mm in (
            ("F.Cu", "B.Cu", 1.5862),
            ("F.Cu", "In1.Cu", 0.2530),
            ("F.Cu", "In2.Cu", 1.3332),
            ("In1.Cu", "In2.Cu", 1.0802),
            ("B.Cu", "F.Cu", 1.5862),
        ):
            self.assertAlmostEqual(lm.layer_distance(st, a, b), mm, places=9)

    def test_no_block_reads_none(self):
        self.assertIsNone(lm.read_stackup("(kicad_pcb (setup (pad_to_mask_clearance 0)))"))

    def test_attach_only_for_pairs_or_groups(self):
        rules = {"diff_pairs": [], "length_match": []}
        self.assertFalse(lm.attach_stackup(rules, SGPS))
        self.assertNotIn("stackup", rules)
        rules = {"diff_pairs": [{"name": "d", "p": "A", "n": "B"}]}
        self.assertTrue(lm.attach_stackup(rules, SGPS))
        self.assertEqual(len(lm.stackup_copper(rules["stackup"])), 4)

    def test_six_layer_inner_span(self):
        st = lm.default_stackup(6)
        gap = (1.6 - 0.02 - 6 * 0.035) / 5
        self.assertAlmostEqual(lm.layer_distance(st, "In1.Cu", "In2.Cu"), gap + 0.035, places=9)


class LengthTest(unittest.TestCase):
    st = lm.default_stackup(2)

    def length(self, tracks, vias=(), pads=(), **kw):
        return lm.net_length("N", tracks, list(vias), list(pads), self.st, **kw)

    def test_straight_and_dogleg(self):
        r = self.length([("F.Cu", (0, 0), (3, 0), 0.25), ("F.Cu", (3, 0), (3, 4), 0.25)])
        self.assertAlmostEqual(r.total_mm, 7.0)
        self.assertEqual(r.via_mm, 0.0)

    def test_two_vias_count_their_span(self):
        tracks = [
            ("F.Cu", (0, 0), (1, 0), 0.25),
            ("B.Cu", (1, 0), (5, 0), 0.25),
            ("F.Cu", (5, 0), (6, 0), 0.25),
        ]
        r = self.length(tracks, vias=[(1, 0), (5, 0)])
        self.assertAlmostEqual(r.total_mm, 6 + 2 * 1.58)

    def test_via_joining_one_layer_counts_nothing(self):
        r = self.length([("F.Cu", (0, 0), (1, 0), 0.25)], vias=[(1, 0)])
        self.assertAlmostEqual(r.total_mm, 1.0)

    def test_wander_in_pad_counts_straight_from_the_centre(self):
        pad = square_pad("N", (0.0, 0.0), 1.0)
        tracks = [
            ("F.Cu", (0, 0), (0, 0.3), 0.25),
            ("F.Cu", (0, 0.3), (0.3, 0.3), 0.25),
            ("F.Cu", (0.3, 0.3), (0.3, 2.0), 0.25),
        ]
        r = self.length(tracks, pads=[pad])
        # Straight from the centre to where the line crosses y = 0.5, then on.
        expected = math.hypot(0.3, 0.5) + 1.5
        self.assertAlmostEqual(r.total_mm, expected, places=6)

    def test_line_off_the_centre_is_not_clipped(self):
        pad = square_pad("N", (0.0, 0.0), 1.0)
        tracks = [("F.Cu", (0.2, 0), (0.2, 0.4), 0.25), ("F.Cu", (0.2, 0.4), (3, 0.4), 0.25)]
        self.assertAlmostEqual(self.length(tracks, pads=[pad]).total_mm, 3.2)

    def test_line_wholly_inside_a_pad_keeps_its_length(self):
        pad = square_pad("N", (0.0, 0.0), 1.0)
        tracks = [
            ("F.Cu", (0, 0), (0, 0.3), 0.25),
            ("F.Cu", (0, 0.3), (0.3, 0.3), 0.25),
            ("B.Cu", (0.3, 0.3), (3, 0.3), 0.25),
        ]
        r = self.length(tracks, vias=[(0.3, 0.3)], pads=[pad])
        self.assertAlmostEqual(r.total_mm, 0.6 + 2.7 + 1.58)

    def test_via_in_a_pad_joins_the_pad_layer(self):
        pad = square_pad("N", (0.0, 0.0), 1.0)
        r = self.length([("B.Cu", (0.2, 0.1), (3, 0.1), 0.25)], vias=[(0.2, 0.1)], pads=[pad])
        self.assertAlmostEqual(r.via_mm, 1.58)

    def test_turn_inside_a_via_counts_straight_from_its_centre(self):
        # KiCad 10.0.6, ladder 07 seed 0 net Q1: 0.25 mm down then 45 degrees out of a
        # 0.6 mm via measures 0.0156 mm less than the raw tracks.
        tracks = [
            ("F.Cu", (-1, 0), (0, 0), 0.25),
            ("B.Cu", (0, 0), (0, -0.25), 0.25),
            ("B.Cu", (0, -0.25), (0.25, -0.5), 0.25),
            ("B.Cu", (0.25, -0.5), (2.0, -2.25), 0.25),
        ]
        raw = 1 + 0.25 + math.hypot(1.75, 1.75) + math.hypot(0.25, 0.25)
        r = self.length(tracks, vias=[(0, 0)], via_radius=0.3)
        self.assertAlmostEqual(raw + 1.58 - r.total_mm, 0.0156, places=4)

    def test_lines_join_only_where_two_ends_meet_on_one_layer(self):
        def lines(tracks):
            out = [lm._Line(layer, w, [lm._key(a), lm._key(b)]) for layer, a, b, w in tracks]
            lm._merge_lines(out)
            return sorted(len(x.pts) for x in out if x.status == 1)

        tracks = [
            ("F.Cu", (0, 0), (1, 0), 0.25),
            ("F.Cu", (1, 0), (2, 0), 0.25),
            ("B.Cu", (1, 0), (1, 1), 0.25),
        ]
        self.assertEqual(lines(tracks), [2, 2, 2])
        self.assertEqual(lines(tracks[:2]), [3])
        # Two lines on one layer join through a via or a pad centre too.
        r = self.length(tracks[:2], vias=[(1, 0)], via_radius=0.3)
        self.assertEqual(len(r.lines), 1)

    def test_board_frame_is_the_writebacks(self):
        from pnr.writeback import to_pcb_nm

        frame = lm.board_frame(34.0)
        for p in ((0.0, 0.0), (12.3456789, 7.0000004), (45.875, 33.125)):
            self.assertEqual(frame(p), to_pcb_nm(p[0], p[1], 34.0))

    def test_delay_uses_the_layer_and_the_barrel(self):
        st = lm.read_stackup(SGPS)
        delay = lm.DelayModel(st)
        tracks = [("F.Cu", (0, 0), (10, 0), 0.25), ("B.Cu", (10, 0), (20, 0), 0.25)]
        r = lm.net_length("N", tracks, [(10, 0)], [], st, delay)
        expected = (
            10 * delay.track("F.Cu", 0.25)
            + 10 * delay.track("B.Cu", 0.25)
            + 1.5862 * delay.barrel_ps_per_mm
        )
        self.assertAlmostEqual(r.delay_ps, expected, places=6)
        self.assertGreater(delay.track("F.Cu", 0.25), 5.0)  # FR4 microstrip, ~5.7 ps/mm


class KicadGoldenTest(unittest.TestCase):
    """Variants of one 2-pin net measured by KiCad 10.0.6's DRC (``length`` rule) on a
    2-layer board without a stackup block (1.58 mm vias, 0.6 mm vias, pcbnew frame).
    The pads are KiCad's 25 % roundrects."""

    pa, pb = (48.9375, 55.375), (61.0, 56.9125)
    st = lm.default_stackup(2)

    def pads(self):
        return [
            lm.PadCopper(
                "N", self.pa, frozenset(("F.Cu",)), lm.rounded_rect(self.pa, (0.975, 1.4), 0.24375)
            ),
            lm.PadCopper(
                "N", self.pb, frozenset(("F.Cu",)), lm.rounded_rect(self.pb, (1.4, 1.025), 0.25625)
            ),
        ]

    def off(self, dx, dy):
        return (self.pa[0] + dx, self.pa[1] + dy)

    def measure(self, chains, vias=()):
        tracks = []
        for layer, pts in chains:
            tracks += [(layer, a, b, 0.25) for a, b in zip(pts, pts[1:])]
        return lm.net_length("N", tracks, list(vias), self.pads(), self.st, via_radius=0.3).total_mm

    def test_variants(self):
        pa, pb = self.pa, self.pb
        p = ((2 * pa[0] + pb[0]) / 3, (2 * pa[1] + pb[1]) / 3 + 2.0)
        q = ((pa[0] + 2 * pb[0]) / 3, (pa[1] + 2 * pb[1]) / 3 + 2.0)
        near = self.off(0.2125, -0.125)  # a via whose copper holds the pad centre
        far = self.off(0.3, -0.5)
        hook = [pa, self.off(-0.0375, 0.0375), self.off(-0.0375, 0.125)]
        cases = {
            "straight": ([("F.Cu", [pa, pb])], [], 12.1601),
            "dogleg": ([("F.Cu", [pa, (pb[0], pa[1]), pb])], [], 13.6000),
            "twovias": (
                [("F.Cu", [pa, p]), ("B.Cu", [p, q]), ("F.Cu", [q, pb])],
                [p, q],
                16.2418,
            ),
            "viainpad": ([("B.Cu", [pa, pb])], [pa, pb], 15.3201),
            "hook": ([("F.Cu", [pa, self.off(-0.2, 0.3), self.off(0.2, 0.3), pb])], [], 12.2265),
            "viahook": (
                [("F.Cu", [pa, self.off(0.2, -0.25)]), ("B.Cu", [self.off(0.2, -0.25), pb])],
                [self.off(0.2, -0.25), pb],
                15.4766,
            ),
            # KiCad clips the in-pad hook at the via, whose copper holds the line's
            # start (the pad centre): via centre -> via edge -> on.
            "r4hook": ([("F.Cu", hook + [near]), ("B.Cu", [near, pb])], [near, pb], 15.8638),
            "r4direct": ([("F.Cu", [pa, near]), ("B.Cu", [near, pb])], [near, pb], 15.3726),
            "r4far": ([("F.Cu", hook + [far]), ("B.Cu", [far, pb])], [far, pb], 15.9485),
            "offcentre": ([("F.Cu", [self.off(0, 0.2), pb])], [], 12.1364),
        }
        for name, (chains, vias, kicad) in cases.items():
            with self.subTest(name):
                self.assertAlmostEqual(self.measure(chains, vias), kicad, delta=6e-5)

    def test_points_on_a_pad_edge_follow_kicads_half_open_rule(self):
        # pcbnew frame (y down): the bottom edge (largest y) is inside, the top is not.
        pad = lm.PadCopper("N", (0.0, 0.0), frozenset(("F.Cu",)), lm.rounded_rect((0, 0), (1, 1)))
        poly = [lm._key(q) for q in pad.outline]
        self.assertTrue(lm._point_inside(poly, lm._key((0.2, 0.5))))
        self.assertFalse(lm._point_inside(poly, lm._key((0.2, -0.5))))
        self.assertTrue(lm._point_inside(poly, lm._key((-0.5, 0.1))))
        self.assertFalse(lm._point_inside(poly, lm._key((0.5, 0.1))))


if __name__ == "__main__":
    unittest.main()
