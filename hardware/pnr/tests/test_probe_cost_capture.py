import json, os, random, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from pnr.place.relocate import distance_field
from pnr.place.batch_relocate import propose_batches
from pnr.graph import BoardGraph, BoardOutline, Component, Pad, Net
from pnr.constraints import compile_constraints


class ProbeCapture(unittest.TestCase):
    def test_path_terms_exact_without_changing_field(self):
        blocked = np.zeros((2, 5, 9), bool)
        copper = np.zeros_like(blocked, float)
        copper[0, :, 4] = 1
        for aperture in (True, False):
            via = np.full((5, 9), aperture)
            base = distance_field(blocked, via, copper, [(0, 2, 0)], 1.0)
            actual, terms = distance_field(blocked, via, copper, [(0, 2, 0)], 1.0, terms=True)
            np.testing.assert_array_equal(base, actual)
            finite = np.isfinite(actual)
            np.testing.assert_allclose(
                actual[finite],
                (terms[..., 0] + 3 * terms[..., 1] + 100 * terms[..., 2])[finite],
                atol=1e-8,
            )
            if aperture:
                self.assertEqual(terms[0, 2, 8].tolist(), [8.0, 2.0, 0.0])
            else:
                self.assertEqual(terms[0, 2, 8].tolist(), [8.0, 0.0, 2.0])

    def test_blocked_unreachable_stays_infinite(self):
        blocked = np.zeros((2, 5, 9), bool)
        blocked[:, :, 4] = True
        dist, terms = distance_field(
            blocked,
            np.ones((5, 9), bool),
            np.zeros_like(blocked, float),
            [(0, 2, 0)],
            1.0,
            terms=True,
        )
        self.assertTrue(np.isinf(dist[0, 2, 8]))
        self.assertFalse(np.isnan(terms).any())

    def test_actual_batch_capture_preserves_choices_and_terms(self):
        a = Component(
            "A", "", (3, 3), 0, "top", (1, 1), (1, 1), pads=[Pad("1", "N", (0, 0), (0.4, 0.4))]
        )
        b = Component(
            "B", "", (17, 13), 0, "top", (1, 1), (1, 1), pads=[Pad("1", "N", (0, 0), (0.4, 0.4))]
        )
        g = BoardGraph(
            "test", [a, b], [Net("N", 1, [("A", "1"), ("B", "1")])], BoardOutline(20, 16)
        )
        cc = compile_constraints(
            {"board": {"outline": {"w": 20, "h": 16}}, "fixed": {"B": {"at": [17, 13]}}}, g.refs
        )

        def run():
            return propose_batches(
                g, cc, {}, [], refs=["A"], k=1, n=3, samples=2, pitch=1.0, rng=random.Random(6)
            )

        with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": ""}):
            base, original = run()
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": td}
        ):
            actual, audit = run()
            docs = [json.loads(p.read_text()) for p in Path(td).glob("*.json")]
            self.assertEqual(len(docs), 1)
            d = docs[0]
            self.assertEqual(d["kind"], "routing-probe")
            self.assertEqual(
                [v["graph"].to_json() for v in base], [v["graph"].to_json() for v in actual]
            )
            self.assertEqual([v["cost"] for v in base], [v["cost"] for v in actual])
            self.assertEqual(
                [v["cost"] for v in original["probes"][0]["candidates"]],
                [v["cost"] for v in audit["probes"][0]["candidates"]],
            )
            c = d["component"]
            self.assertAlmostEqual(c["total"], sum(t["weighted"] for t in c["terms"]))
            self.assertEqual(c["position"], [3, 3])
            self.assertEqual(d["decision_context"]["held_out"], ["A"])
            for row in d["recorded_fields"][0]["values"]:
                self.assertAlmostEqual(row[2], sum(row[3:]), places=7)
            for choice in actual:
                self.assertAlmostEqual(choice["cost"], sum(t["weighted"] for t in choice["terms"]))


if __name__ == "__main__":
    unittest.main()
