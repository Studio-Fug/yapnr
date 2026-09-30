"""Fine-pitch escape reservation: pitch, direction, conflicts and joint choice."""

import math
import unittest
from pnr import fanout_reserve as F

G = dict(clearance=0.15, via=0.6, drill=0.3, hole=0.2)


def escape(net, points, via=None, cost=0.0, width=0.2, layer=0):
    pts = [tuple(p) for p in points] + ([tuple(via)] if via else [])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    r = G["via"] / 2 if via else 0
    return dict(
        net=net,
        points=[tuple(p) for p in points],
        via=via,
        cost=cost,
        width=width,
        layer=layer,
        box=(min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r),
    )


class FanoutReserveTest(unittest.TestCase):
    def test_modal_congruent_pitch_ignores_odd_lands(self):
        row = [((i * 0.5, 0), (0.25, 0.7)) for i in range(6)] + [
            ((i * 0.5 + 0.25, 3), (0.7, 0.25)) for i in range(3)
        ]
        strips = [((0, 1.5), (0.38, 1.43)), ((0.62, 1.5), (0.38, 1.43))]
        self.assertAlmostEqual(F.row_pitch(row + strips + [((1, 1), (2.6, 2.6))]), 0.5)
        self.assertEqual(F.row_pitch([((0, 0), (1, 1)), ((5, 0), (2, 1))]), math.inf)

    def test_outward_is_pad_aligned_and_away_from_body(self):
        self.assertEqual(F.outward((3, 0), (1, 0), 0.3, 0.1, (0, 0)), (1, 0))
        self.assertEqual(F.outward((-3, 0), (1, 0), 0.3, 0.1, (0, 0)), (-1, 0))
        # A long land centred on the body axis (exposed pad) has no outward side.
        self.assertIsNone(F.outward((0, 1), (1, 0), 1.3, 0.7, (0, 0)))
        u = F.outward((0.2, 0.2), (1, 0), 0.1, 0.1, (0, 0))
        self.assertAlmostEqual(u[0], math.sqrt(0.5))
        self.assertAlmostEqual(u[1], math.sqrt(0.5))
        h, half = F.extent((0, 1), (1, 0), 0.3, 0.1)
        self.assertAlmostEqual(h, 0.1)
        self.assertAlmostEqual(half, 0.3)

    def test_perimeter_exit_distance(self):
        box = (0, 0, 4, 4)
        self.assertAlmostEqual(F.exit_distance((4, 2), (1, 0), box), 0)
        self.assertAlmostEqual(F.exit_distance((3, 2), (1, 0), box), 1)
        self.assertAlmostEqual(F.exit_distance((5, 2), (1, 0), box), 0)

    def test_recessed_outer_row_is_perimeter_despite_pose_rounding(self):
        # A side land 0.05 mm inside the ring at x=63.6 rounds to 0.05000000000000426.
        x = 63.6
        ring = (0, 0, x + 0.41, 10)
        self.assertGreater(F.exit_distance(F.ahead((x, 5), (1, 0), 0.36), (1, 0), ring), 0.05)
        self.assertTrue(F.on_ring((x, 5), (1, 0), 0.36, ring))
        self.assertTrue(F.on_ring((5, x), (0, 1), 0.36, (0, 0, 10, x + 0.41)))
        # An inner row or exposed pad stays interior.
        self.assertFalse(F.on_ring((x, 5), (1, 0), 0.3, ring))
        self.assertFalse(F.on_ring((x - 0.915, 5), (1, 0), 0.41, ring))

    def test_parallel_stubs_at_half_mm_pitch_are_compatible_but_side_vias_clash(self):
        a = escape("a", [(0, 0), (1, 0)])
        b = escape("b", [(0, 0.5), (1, 0.5)])
        self.assertFalse(F.clash(a, b, G))
        self.assertTrue(F.clash(escape("a", [(0, 0), (1, 0)], via=(1, 0)), b, G))
        # Staggered: via beyond the neighbour's stub end is legal.
        self.assertFalse(
            F.clash(
                escape("a", [(0, 0), (1.6, 0)], via=(1.6, 0)),
                escape("b", [(0, 0.5), (0.9, 0.5)]),
                G,
            )
        )
        # Different layers only interact through vias.
        self.assertFalse(F.clash(a, escape("b", [(0, 0), (1, 0)], layer=31), G))

    def test_same_net_escapes_only_need_hole_spacing(self):
        a = escape("n", [(0, 0), (1, 0)], via=(1, 0))
        b = escape("n", [(0, 0.4), (1.2, 0.4)], via=(1.2, 0.4))
        self.assertTrue(F.clash(a, b, G))
        self.assertFalse(F.clash(a, escape("n", [(0, 0.8), (1, 0.8)], via=(1, 0.8)), G))

    def test_joint_choice_maximizes_reserved_pads_then_cost(self):
        # Pad a's cheap via blocks pad b entirely; the joint optimum takes a's
        # dearer option so both pads keep an escape.
        a_cheap = escape("a", [(0, 0), (1, 0)], via=(1, 0), cost=0.5)
        a_far = escape("a", [(0, 0), (1.7, 0)], via=(1.7, 0), cost=1.2)
        b_only = escape("b", [(0, 0.5), (0.9, 0.5)], cost=1.0)
        chosen, stats = F.choose({"A.1": [a_cheap, a_far], "B.1": [b_only], "C.1": []}, G)
        self.assertEqual(set(chosen), {"A.1", "B.1"})
        self.assertIs(chosen["A.1"], a_far)
        self.assertEqual(stats["truncated"], 0)

    def test_choice_is_bounded(self):
        rows = {
            f"P.{i}": [
                escape(
                    f"n{i}",
                    [(0, i * 0.5), (1 + 0.25 * k, i * 0.5)],
                    via=(1 + 0.25 * k, i * 0.5),
                    cost=k,
                )
                for k in range(4)
            ]
            for i in range(8)
        }
        chosen, stats = F.choose(rows, G, node_limit=50)
        self.assertLessEqual(stats["nodes"], 50 * stats["clusters"])
        nets = list(chosen.values())
        self.assertFalse(any(F.clash(x, y, G) for i, x in enumerate(nets) for y in nets[i + 1 :]))

    def test_lanes_keep_power_pad_projection_free(self):
        lane = dict(net="pwr", a=(0, 0.5), z=(1.35, 0.5), half=0.125)
        self.assertTrue(F.lane_clear([lane], "sig", (0, 0), (1, 0), 0.1 + 0.152))
        self.assertFalse(F.lane_clear([lane], "sig", (1, 0), (1, 0), 0.3 + 0.152))
        self.assertTrue(F.lane_clear([lane], "pwr", (1, 0.5), (1, 0.5), 0.3 + 0.152))


if __name__ == "__main__":
    unittest.main()
