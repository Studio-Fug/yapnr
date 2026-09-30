import random
import unittest

from pnr.route.detail.joint import conflict
from pnr.route.detail.regional import Request
from pnr.route.detail.spatial_conflicts import PrimitiveIndex


class SpatialTest(unittest.TestCase):
    def test_exact_equivalence_to_all_pairs_with_different_layers_widths_nets(self):
        rng = random.Random(142)
        requests = [
            Request(
                str(i),
                str(i % 4),
                [],
                [],
                rng.choice([0.1, 0.2, 0.5, 1.5]),
                rng.choice([0.1, 0.151, 0.4]),
            )
            for i in range(12)
        ]

        def primitive():
            a = (rng.uniform(-3, 9), rng.uniform(-3, 9))
            if rng.random() < 0.25:
                return ("via", None, a, a)
            b = (a[0] + rng.uniform(-3, 3), a[1] + rng.uniform(-3, 3))
            return ("track", rng.randrange(3), a, b)

        entries = [(rng.choice(requests), primitive()) for _ in range(160)]
        index = PrimitiveIndex(entries, conflict)
        for _ in range(2000):
            r, p = rng.choice(requests), primitive()
            self.assertEqual(index.collides(r, p), any(conflict(r, p, q, c) for q, c in entries))

    def test_same_net_coincident_barrel_and_separation(self):
        r = Request("a", "N", [], [])
        p = ("via", None, (1.0, 1.0), (1.0, 1.0))
        idx = PrimitiveIndex([(r, p)], conflict)
        self.assertFalse(idx.collides(r, p))
        self.assertTrue(idx.collides(r, ("via", None, (1.4, 1.0), (1.4, 1.0))))


if __name__ == "__main__":
    unittest.main()
