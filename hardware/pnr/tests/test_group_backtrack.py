import unittest, math, os, json, tempfile
from pathlib import Path
from unittest.mock import patch
from pnr.graph import BoardGraph, BoardOutline, Component
from pnr.place.legalize import legalize, LegalizationError
from pnr.place.geometry import Rect
from pnr.place.metrics import overlap_pairs, outside_outline


class GroupBacktrackTest(unittest.TestCase):
    def fixture(self):
        poses = [
            (7.268057595224243, 7.6816966717879644),
            (1.6852276165430302, 4.88792370653291),
            (1.5537001477470689, 7.0848173220578525),
            (7.126675434455902, 2.0271317159981024),
            (4.8022590247898505, 5.398428747959551),
        ]
        g = BoardGraph(
            "packing",
            [
                Component(
                    "U1" if i == 0 else "C" + str(i),
                    "",
                    p,
                    0,
                    "top",
                    (2, 2 if i == 0 else 1),
                    (2, 2 if i == 0 else 1),
                )
                for i, p in enumerate(poses)
            ],
            [],
            BoardOutline(10, 10),
        )
        return g, dict(
            fixed={},
            keepouts=[Rect(3.120453031520473, 7.9794643286820595, 2, 3)],
            grid_mm=0.5,
            group_edges=[("U1", "C" + str(i), 3.0) for i in range(1, 5)],
        )

    def test_backtracking_recovers_greedy_dead_end(self):
        g, kw = self.fixture()
        original = g.to_json()
        with self.assertRaises(LegalizationError):
            legalize(g, 10, 10, **kw, backtrack_budget=0)
        p = legalize(g, 10, 10, **kw, backtrack_budget=100)
        self.assertEqual(g.to_json(), original)
        self.assertFalse(overlap_pairs(p))
        self.assertFalse(outside_outline(p, 10, 10))
        for i in range(1, 5):
            self.assertLessEqual(
                math.dist(p.component("U1").pos, p.component("C" + str(i)).pos), 3 + 1e-9
            )
        self.assertEqual(p.to_json(), legalize(g, 10, 10, **kw, backtrack_budget=100).to_json())

    def test_capture_keeps_only_final_choices_and_preserves_result(self):
        g, kw = self.fixture()
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": "", "PNR_LIVE_DIR": ""}):
                a = legalize(g, 10, 10, **kw, backtrack_budget=100)
            with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": d, "PNR_LIVE_DIR": ""}):
                b = legalize(g, 10, 10, **kw, backtrack_budget=100)
            self.assertEqual(a.to_json(), b.to_json())
            records = [json.loads(p.read_text()) for p in Path(d).glob("*.json")]
            decisions = [v for v in records if v["kind"] == "legalizer-decision"]
            self.assertEqual(len(decisions), 5)
            complete = next(v for v in records if v["kind"] == "legalizer-complete")
            self.assertGreater(complete["backtracks"], 0)
            for v in decisions:
                self.assertEqual(tuple(v["position"]), b.component(v["ref"]).pos)
                self.assertAlmostEqual(v["total"], sum(t["weighted"] for t in v["terms"]))

    def test_exhaustion_never_publishes_accepted_partial_choices(self):
        g, kw = self.fixture()
        with tempfile.TemporaryDirectory() as d, patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": d, "PNR_LIVE_DIR": ""}
        ):
            with self.assertRaises(LegalizationError):
                legalize(g, 10, 10, **kw, backtrack_budget=0)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
