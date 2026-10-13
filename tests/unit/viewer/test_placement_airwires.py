import copy
import unittest

from yapnr.viewer.services.placement_airwires import spans
from yapnr.viewer.services.placement_render import render


class Airwires(unittest.TestCase):
    def test_minimum_tree_rotated_pads_duplicates_and_missing_pins(self):
        # Pad endpoints form a 3-4-5 triangle; the tree must use 3 + 4, not 3 + 5.
        graph = dict(
            components=[
                dict(ref="A", pos=[0, -1], rot=90, pads=[dict(name="1", offset=[1, 0])]),
                dict(ref="B", pos=[3, 0], rot=0, pads=[dict(name="1", offset=[0, 0])]),
                dict(ref="C", pos=[0, 4], rot=0, pads=[dict(name="1", offset=[0, 0])]),
            ],
            nets=[
                dict(
                    name="N<&",
                    pins=[["A", "1"], ["B", "1"], ["C", "1"], ["C", "1"], ["missing", "1"]],
                )
            ],
        )
        before = copy.deepcopy(graph)
        tree = spans(graph)
        self.assertAlmostEqual(tree["length_mm"], 7)
        self.assertEqual(len(tree["nets"][0]["edges"]), 2)
        self.assertEqual(tree["nets"][0]["missing_pins"], [["missing", "1"]])
        self.assertEqual(graph, before)
        self.assertIn("N&lt;&amp;", render(graph))
        self.assertNotIn("<path", render(graph, airwires=False))
        graph["components"].reverse()
        graph["nets"][0]["pins"].reverse()
        self.assertEqual(spans(graph), tree)

    def test_logical_pin_uses_nearest_physical_pad_and_decision_pose(self):
        graph = dict(
            components=[
                dict(
                    ref="A",
                    pos=[0, 0],
                    pads=[dict(name="1", offset=[0, 0]), dict(name="1", offset=[9, 0])],
                ),
                dict(ref="B", pos=[10, 0], pads=[dict(name="1", offset=[0, 0])]),
            ],
            nets=[dict(name="N", pins=[["A", "1"], ["A", "1"], ["B", "1"]])],
        )
        tree = spans(graph)
        self.assertEqual(tree["length_mm"], 1)
        self.assertEqual(tree["nets"][0]["edges"][0]["start"], [9, 0])
        decision = dict(ref="B", position=[12, 0], rotation=90)
        self.assertEqual(spans(graph, decision)["length_mm"], 3)
        decision["moves"] = [
            dict(ref="A", before=[0, 0], before_rotation=0, after=[2, 0], rotation=0)
        ]
        self.assertEqual(spans(graph, decision)["length_mm"], 1)


if __name__ == "__main__":
    unittest.main()
