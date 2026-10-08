import unittest

from pnr.constraints import ConstraintError, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.line_group import reorder, violations


def fixture(allow=True):
    parts = [
        Component(
            ref,
            "series",
            (x, 10),
            90,
            "top",
            (1, 1),
            (1, 1),
            pads=[
                Pad("1", net, (-0.3, 0), (0.3, 0.3)),
                Pad("2", net + "_out", (0.3, 0), (0.3, 0.3)),
            ],
        )
        for ref, x, net in [("R1", 10, "P"), ("R2", 12, "N")]
    ]
    parts.append(
        Component(
            "U1",
            "sink",
            (11, 5),
            0,
            "top",
            (3, 1),
            (3, 1),
            pads=[
                Pad("1", "P", (1, 0), (0.3, 0.3)),
                Pad("2", "N", (-1, 0), (0.3, 0.3)),
            ],
        )
    )
    graph = BoardGraph(
        "reorder",
        parts,
        [
            Net("P", 1, [("R1", "1"), ("U1", "1")]),
            Net("N", 2, [("R2", "1"), ("U1", "2")]),
        ],
        BoardOutline(24, 24),
    )
    doc = {
        "board": {"outline": {"w": 24, "h": 24}, "default_clearance_mm": 0.2},
        "line_group": [
            dict(
                name="series",
                members=["R1", "R2"],
                pitch_mm=2,
                rot=90,
                allow_reorder=allow,
            )
        ],
    }
    return (
        graph,
        compile_constraints(doc, graph.refs),
        {"diff_pairs": [dict(name="p", p="P", n="N")]},
    )


class LineReorderTest(unittest.TestCase):
    def test_half_turn_and_reversal_flip_pads_without_swapping_slots(self):
        g, c, rules = fixture()
        g.component("U1").pos = (11, 15)
        g.component("U1").pads[0].offset = (-1, 0)
        g.component("U1").pads[1].offset = (1, 0)
        before = {r: g.component(r).pos for r in ("R1", "R2")}
        reorder(g, c, rules)
        self.assertEqual({r: g.component(r).pos for r in before}, before)
        self.assertEqual([g.component(r).rot for r in before], [270, 270])
        self.assertEqual(violations(g, c), [])

    def test_swaps_slots_to_remove_crossing_without_changing_pins(self):
        g, c, r = fixture()
        pins = [(p.name, p.net) for x in g.components for p in x.pads]
        reorder(g, c, r)
        self.assertEqual(g.component("R1").pos, (12, 10))
        self.assertEqual(g.component("R2").pos, (10, 10))
        self.assertEqual(g.component("R1").rot, 90)
        self.assertEqual(pins, [(p.name, p.net) for x in g.components for p in x.pads])
        self.assertEqual(violations(g, c), [])

    def test_existing_unrelated_findings_do_not_block_a_slot_swap(self):
        g, c, rules = fixture()
        g.components += [
            Component(r, "other", (25, 25), 0, "top", (1, 1), (1, 1)) for r in ("X1", "X2")
        ]
        reorder(g, c, rules)
        self.assertEqual(g.component("R1").pos, (12, 10))
        self.assertEqual(g.component("R2").pos, (10, 10))

    def test_ordered_line_is_unchanged_and_still_enforces_order(self):
        g, c, r = fixture(False)
        reorder(g, c, r)
        self.assertEqual(g.component("R1").pos, (10, 10))
        g.component("R1").pos, g.component("R2").pos = (12, 10), (10, 10)
        self.assertEqual(violations(g, c), ["R1", "R2"])

    def test_unordered_does_not_relax_spacing_or_orientation(self):
        g, c, r = fixture()
        g.component("R1").pos = (12, 10)
        g.component("R2").pos = (10, 10.5)
        self.assertEqual(violations(g, c), ["R1", "R2"])

    def test_bad_boolean_rejected(self):
        g, c, r = fixture()
        with self.assertRaises(ConstraintError):
            compile_constraints(
                {"line_group": [dict(name="g", members=["R1", "R2"], allow_reorder="yes")]},
                g.refs,
            )


if __name__ == "__main__":
    unittest.main()
