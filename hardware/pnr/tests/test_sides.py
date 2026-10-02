"""Side policy (``board.sides``), side options and the side cost (pnr.place.sides)."""

import unittest

from pnr.constraints import ConstraintError, compile_constraints
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.place import sides


def part(ref, nets, through=False, size=(2, 1), pos=(5, 5)):
    pads = [
        Pad(str(i + 1), net, (i * 1.0 - 0.5, 0.25), (0.6, 0.6), through)
        for i, net in enumerate(nets)
    ]
    return Component(ref, "test:" + ref, pos, 0, "top", size, size, pads=pads)


def board(*parts):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "t", list(parts), [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))]
    )


def compiled(graph, doc):
    doc = dict(doc)
    doc.setdefault("board", {"outline": {"w": 20, "h": 20}})
    return compile_constraints(doc, graph.refs)


class PolicyTest(unittest.TestCase):
    def test_board_sides_defaults_to_single_and_validates(self):
        g = board(part("R1", ["A", "B"]))
        self.assertEqual(compiled(g, {}).board.sides, "single")
        self.assertEqual(
            compiled(g, {"board": {"outline": {"w": 9, "h": 9}, "sides": "double"}}).board.sides,
            "double",
        )
        with self.assertRaises(ConstraintError):
            compiled(g, {"board": {"sides": "both"}})

    def test_with_policy_maps_only_double_and_never_mutates(self):
        doc = {"board": {"outline": {"w": 9, "h": 9}}, "fixed": {}}
        self.assertEqual(sides.with_policy(doc, "single"), doc)
        self.assertEqual(sides.with_policy(doc, "assigned"), doc)
        mapped = sides.with_policy(doc, "double")
        self.assertEqual(mapped["board"]["sides"], "double")
        self.assertNotIn("sides", doc["board"])
        self.assertEqual(sides.with_policy(None, "double"), {"board": {"sides": "double"}})

    def test_routing_rules_do_not_carry_the_policy(self):
        from pnr.constraints import compile_routing_rules

        g = board(part("R1", ["A", "B"]))
        single = compile_routing_rules(compiled(g, {}), ["A", "B"])
        double = compile_routing_rules(
            compiled(g, {"board": {"outline": {"w": 20, "h": 20}, "sides": "double"}}), ["A", "B"]
        )
        self.assertEqual(single, double)


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.graph = board(
            part("U1", ["A", "B", "C", "D", "E", "F"]),
            part("J1", ["A", "B"], through=True),
            part("C1", ["A", "B"]),
            part("C2", ["C", "D"]),
            part("C3", ["E", "F"]),
            part("R1", ["C", "E"]),
            part("R2", ["D", "F"]),
            part("R3", ["A", "C"]),
        )

    def test_single_policy_frees_nothing(self):
        plan = sides.plan(self.graph, compiled(self.graph, {}))
        self.assertFalse(plan.active)
        self.assertEqual(plan.free, ())
        self.assertEqual(plan.options["C1"], ("top",))

    def test_double_policy_frees_unheld_surface_parts(self):
        doc = {
            "board": {"outline": {"w": 20, "h": 20}, "sides": "double"},
            "fixed": {"U1": {"at": [10, 10]}},
            "side": {"bottom": ["C3"]},
            "keepout": [{"name": "k", "ref": "R1", "extent": {"edge": "north", "depth_mm": 1}}],
            "row": [{"members": ["R2", "R3"], "edge": "any"}],
        }
        plan = sides.plan(self.graph, compiled(self.graph, doc))
        self.assertEqual(plan.free, ("C1", "C2"))
        self.assertEqual(plan.options["C1"], ("top", "bottom"))
        self.assertEqual(plan.options["C3"], ("bottom",))
        self.assertEqual(
            plan.held,
            dict(
                U1="fixed",
                J1="drilled",
                C3="hard_side",
                R1="keepout",
                R2="line_or_row",
                R3="line_or_row",
            ),
        )

    def test_locked_and_landing_parts_are_held(self):
        self.graph.component("C1").locked = True
        self.graph.component("C2").reserves = [dict(pad="1")]
        plan = sides.plan(self.graph, compiled(self.graph, {"board": {"sides": "double"}}))
        self.assertEqual(plan.held["C1"], "locked")
        self.assertEqual(plan.held["C2"], "landing_reserve")

    def test_side_pref_frees_a_part_under_single(self):
        plan = sides.plan(self.graph, compiled(self.graph, {"side_pref": {"bottom": ["C*", "J1"]}}))
        self.assertEqual(plan.free, ("C1", "C2", "C3"))
        self.assertEqual(plan.preferred["C1"], ("bottom", 1.0))
        self.assertEqual(plan.held, {"J1": "drilled"})
        self.assertEqual(sides.preference_cost(plan, {"C1": "top"}), sides.SIDE_PREF_MM)
        self.assertEqual(sides.preference_cost(plan, {"C1": "bottom"}), 0.0)


class CostTest(unittest.TestCase):
    def test_split_surface_net_costs_a_via_and_a_drilled_pin_none(self):
        g = board(
            part("U1", ["A", "B", "C", "D", "E", "F"]),
            part("C1", ["A", "B"]),
            part("J1", ["B", "G"], through=True),
        )
        plan = sides.plan(g, compiled(g, {"board": {"sides": "double"}}))
        self.assertEqual(plan.free, ("C1", "U1"))
        self.assertEqual(sides.side_cost(g, plan), 0.0)
        flipped = {"U1": "top", "C1": "bottom", "J1": "top"}
        # A is split (surface pins on both sides); B has J1's drilled pin.
        self.assertEqual(sides.split_nets(g, plan, flipped), ["A"])
        self.assertEqual(sides.side_cost(g, plan, flipped), sides.VIA_MM + sides.FLIP_MM)

    def test_plane_nets_cost_no_via(self):
        g = board(part("U1", ["A", "B", "C", "D", "E", "F"]), part("C1", ["A", "B"]))
        doc = {
            "board": {"layers": 4, "sides": "double"},
            "net_class": {"gnd": {"nets": ["A", "B"], "plane_layer": "In1.Cu"}},
        }
        plan = sides.plan(g, compiled(g, doc))
        self.assertEqual(sides.split_nets(g, plan, {"U1": "top", "C1": "bottom"}), [])

    def test_assign_mirrors_pads_and_refuses_held_parts(self):
        g = board(part("C1", ["A", "B"]), part("J1", ["A", "B"], through=True))
        plan = sides.plan(g, compiled(g, {"board": {"sides": "double"}}))
        self.assertEqual(sides.assign(g, {"C1": "bottom"}, plan), ["C1"])
        self.assertEqual(g.component("C1").pads[0].offset, (-0.5, -0.25))
        with self.assertRaises(ValueError):
            sides.assign(g, {"J1": "bottom"}, plan)
        sides.check_held(g, plan)
        g.component("J1").side = "bottom"
        with self.assertRaises(ValueError):
            sides.check_held(g, plan)

    def test_same_footprint_modulo_mirror(self):
        a = part("C1", ["A", "B"])
        b = part("C1", ["A", "B"])
        self.assertTrue(sides.same_footprint(b, a))
        from pnr.place.geometry import set_component_side

        set_component_side(b, "bottom")
        self.assertTrue(sides.same_footprint(b, a))
        b.pads[0].offset = (9.0, 9.0)
        self.assertFalse(sides.same_footprint(b, a))

    def test_report_names_bottom_and_flipped_parts(self):
        g = board(part("U1", ["A", "B", "C", "D", "E", "F"]), part("C1", ["A", "B"]))
        plan = sides.plan(g, compiled(g, {"board": {"sides": "double"}}))
        sides.assign(g, {"C1": "bottom"}, plan)
        out = sides.report(g, plan)
        self.assertEqual(out["policy"], "double")
        self.assertEqual(out["bottom"], ["C1"])
        self.assertEqual(out["flipped"], ["C1"])
        self.assertEqual(out["split_nets"], ["A", "B"])

    def test_under_body_sides_puts_two_pin_parts_under_their_ic(self):
        g = board(
            part("U1", ["A", "B", "C", "D", "E", "F"]),
            part("C1", ["A", "B"]),
            part("R1", ["X", "Y"]),
        )
        plan = sides.plan(g, compiled(g, {"board": {"sides": "double"}}))
        self.assertEqual(sides.under_body_sides(g, plan), {"C1": ("bottom", "U1")})


if __name__ == "__main__":
    unittest.main()
