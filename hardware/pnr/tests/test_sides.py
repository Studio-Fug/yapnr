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
        with self.assertRaises(ConstraintError):
            compiled(g, {"board": {"layers": 1, "sides": "double"}})
        self.assertEqual(compiled(g, {"board": {"layers": 1}}).board.sides, "single")

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

    def test_side_pref_is_ignored_and_warned_under_single(self):
        c = compiled(self.graph, {"side_pref": {"bottom": ["C*", "J1"]}})
        self.assertTrue(any("side_pref" in w and "double" in w for w in c.warnings), c.warnings)
        plan = sides.plan(self.graph, c)
        self.assertFalse(plan.active)
        self.assertEqual(plan.preferred, {})
        self.assertEqual(plan.held, {})
        self.assertEqual(sides.stack_refs(self.graph, c), frozenset())

    def test_side_pref_is_a_bias_under_double(self):
        doc = {"board": {"sides": "double"}, "side_pref": {"bottom": ["C*", "J1"]}}
        c = compiled(self.graph, doc)
        self.assertFalse(any("side_pref" in w for w in c.warnings), c.warnings)
        plan = sides.plan(self.graph, c)
        self.assertEqual(plan.free, ("C1", "C2", "C3", "R1", "R2", "R3", "U1"))
        self.assertEqual(plan.preferred["C1"], ("bottom", 1.0))
        self.assertEqual(plan.held, {"J1": "drilled"})
        self.assertEqual(sides.preference_cost(plan, {"C1": "top"}), sides.SIDE_PREF_MM)
        self.assertEqual(sides.preference_cost(plan, {"C1": "bottom"}), 0.0)

    def test_matched_net_parts_are_held(self):
        doc = {
            "board": {"sides": "double"},
            "diff_pair": [{"name": "usb", "p": "A", "n": "B"}],
            "length_match": [{"name": "bus", "nets": ["E*", "Z"]}],
        }
        plan = sides.plan(self.graph, compiled(self.graph, doc))
        # A/B (pair) and E (glob) reach U1, C1, C3, R1 and R3; C2 and R2 stay free.
        self.assertEqual(plan.free, ("C2", "R2"))
        for ref in ("U1", "C1", "C3", "R1", "R3"):
            self.assertEqual(plan.held[ref], "matched_net", ref)
        self.assertEqual(plan.held["J1"], "drilled")

    def test_edge_align_side_holds_and_is_applied_under_double_only(self):
        doc = {"edge_align": {"C1": {"edge": "west", "side": "bottom"}}}
        single = sides.plan(self.graph, compiled(self.graph, doc))
        self.assertEqual(single.options["C1"], ("top",))
        self.assertEqual(sides.apply_held(self.graph, single), [])
        doc["board"] = {"sides": "double"}
        plan = sides.plan(self.graph, compiled(self.graph, doc))
        self.assertEqual(plan.options["C1"], ("bottom",))
        self.assertEqual(plan.held["C1"], "edge_side")
        self.assertEqual(sides.apply_held(self.graph, plan), ["C1"])
        self.assertEqual(self.graph.component("C1").side, "bottom")
        self.assertEqual(self.graph.component("C1").pads[0].offset, (-0.5, -0.25))
        sides.check_held(self.graph, plan)

    def test_stacking_rule_follows_the_policy_alone(self):
        # Under double the rule covers every part that fans out, even when routing
        # rules hold the only free part (every checker sees the same rule).
        doc = {"board": {"sides": "double"}, "fixed": {"U1": {"at": [10, 10]}}}
        c = compiled(self.graph, doc)
        held_all = {
            "plane_access_intents": [
                {"ref": r, "kind": "x"} for r in ("C1", "C2", "C3", "R1", "R2", "R3")
            ]
        }
        self.assertFalse(sides.plan(self.graph, c, held_all).active)
        self.assertEqual(sides.stack_refs(self.graph, c), frozenset({"U1"}))
        self.assertEqual(sides.stack_refs(self.graph, compiled(self.graph, {})), frozenset())


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

    def test_report_names_bottom_flipped_and_assigned_parts(self):
        g = board(
            part("U1", ["A", "B", "C", "D", "E", "F"]),
            part("C1", ["A", "B"]),
            part("C2", ["C", "D"]),
        )
        doc = {"board": {"sides": "double"}, "side": {"bottom": ["C2"]}}
        plan = sides.plan(g, compiled(g, doc))  # from the source: C2 still on top
        sides.assign(g, {"C1": "bottom"}, plan)
        from pnr.place.geometry import set_component_side

        set_component_side(g.component("C2"), "bottom")  # as the placer applies the rule
        out = sides.report(g, plan)
        self.assertEqual(out["policy"], "double")
        self.assertEqual(out["bottom"], ["C1", "C2"])
        self.assertEqual(out["flipped"], ["C1"])
        self.assertEqual(out["assigned"], ["C2"])
        self.assertEqual(out["split_nets"], ["A", "B", "C", "D"])

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
