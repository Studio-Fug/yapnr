import unittest

import pcbnew as k
from pnr.native_electrical import Oracle, pair_layer_bridge
from pnr.route.detail.coupled import path_metrics
from test_pair_bridge import PairBridgeTest


class IntermediateTimingTest(PairBridgeTest):
    def test_bookkeeping_uses_actual_lengths_not_search_target(self):
        b, terms, r, pair = self.setup_bridge()
        o = Oracle(b, r)
        target = 0.3
        plan = pair_layer_bridge(
            b, pair, terms, r, o, (1, 1, 19, 19), 0.2, {}, timing_target_mm=target
        )
        self.assertEqual(plan["status"], "routed")
        measured = {}
        for net in ("p", "n"):
            metric = path_metrics(
                [(la, a, z) for nn, la, a, z, w in plan["pair_tracks"] if nn == net],
                [(pt, [k.F_Cu, k.B_Cu]) for nn, pt in plan["pair_vias"] if nn == net],
                (terms[net][0], k.F_Cu),
                (terms[net][1], k.F_Cu),
                layer_heights={k.F_Cu: 0, k.B_Cu: 1.6},
            )
            self.assertTrue(metric["valid"])
            self.assertAlmostEqual(metric["length_mm"], plan["lengths"][net], places=5)
            measured[net] = metric["length_mm"]
        self.assertLessEqual(abs(measured["p"] - measured["n"] - target), pair["skew_mm"] + 1e-6)
        self.assertGreater(abs(measured["p"] - measured["n"]), pair["skew_mm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
