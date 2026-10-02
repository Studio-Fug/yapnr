"""The board's own copper stack (pnr.stack): roles, legacy equality, rung consistency."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))

from designs import designs, showcases  # noqa: E402
from hard_rungs import dru_text, hard_rungs  # noqa: E402
from stackup import zone_layers  # noqa: E402

from pnr.constraints import compile_constraints, compile_routing_rules  # noqa: E402
from pnr.graph import BoardGraph  # noqa: E402
from pnr.stack import (  # noqa: E402
    PLANE,
    SIGNAL,
    SPLIT,
    UNUSED,
    copper_names,
    grid_layers,
    legacy_layers,
    plane_nets,
    record_from_rows,
    resolve,
)


def rules_of(spec):
    refs = [p["ref"] for p in spec["parts"]]
    nets = sorted({n for p in spec["parts"] for n in p["pins"].values() if n})
    return compile_routing_rules(compile_constraints(spec["constraints"], refs), nets)


def record(code, zones=None, kinds=None):
    """A stack record from a role string (S signal, G/P plane, M mixed, J jumper)."""
    names = copper_names(len(code))
    kind = {"S": "signal", "G": "power", "P": "power", "M": "mixed", "J": "jumper"}
    rows = []
    for index, (name, role) in enumerate(zip(names, code)):
        rows.append(
            dict(
                name=name,
                type=(kinds or {}).get(name, kind[role]),
                copper_mm=0.035 if index in (0, len(code) - 1) else 0.0152,
                zones=(zones or {}).get(name, []),
            )
        )
    return record_from_rows(rows)


def plane_rules(layers, planes):
    """Minimal routing rules: ``planes`` maps net -> class plane layer."""
    return dict(
        layers=layers,
        fab=dict(track_width_mm=0.25),
        net_classes=[
            dict(name="plane_" + net.lower(), nets=[net], plane_layer=layer, width_mm=0.4)
            for net, layer in planes.items()
        ],
    )


class LegacyEquality(unittest.TestCase):
    def test_gate_and_showcases_keep_the_router_heuristic(self):
        from pnr.route.detail.router import _plane_nets, _signal_layers

        for spec in designs() + showcases():
            rules = rules_of(spec)
            with self.subTest(case=spec["name"]):
                # No stack record (no stackup block): the old heuristic, unchanged.
                self.assertEqual(grid_layers(rules, None), _signal_layers(rules))
                self.assertEqual(legacy_layers(rules), _signal_layers(rules))
                self.assertEqual(set(plane_nets(rules, None)), _plane_nets(rules))
                self.assertIsNone(resolve(rules, None))

    def test_rule_planes_on_signal_typed_layers_stay_legacy(self):
        # A product board: a stackup block with every layer typed signal, and its
        # planes from net classes. The stack does not apply.
        rules = plane_rules(4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        self.assertIsNone(resolve(rules, record("SSSS")))
        self.assertEqual(grid_layers(rules, record("SSSS")), legacy_layers(rules))

    def test_a_stack_of_another_layer_count_stays_legacy(self):
        # An atopile layout saved two-layer for a four-layer build (no plane class).
        self.assertIsNone(resolve(plane_rules(4, {}), record("SS")))
        self.assertEqual(grid_layers(plane_rules(4, {}), record("SS")), ("F.Cu", "B.Cu"))
        self.assertIsNotNone(resolve(plane_rules(4, {}), record("SSSS")))

    def test_graph_round_trip_omits_an_absent_stack(self):
        g = BoardGraph(name="x")
        self.assertNotIn("stack", g.to_json())
        g.stack = record("SGPS")
        again = BoardGraph.from_json(g.to_json())
        self.assertEqual(again.stack, g.stack)
        self.assertEqual(again.to_json(), g.to_json())


class Roles(unittest.TestCase):
    def test_two_layer_stack_routes_both_outer_layers(self):
        stack = resolve(plane_rules(2, {}), record("SS"))
        self.assertEqual(stack.grid_layers, ("F.Cu", "B.Cu"))
        self.assertEqual(stack.dedicated, ())

    def test_four_layer_variants(self):
        cases = {
            "SGPS": ({"GND": "In1.Cu", "VCC": "In2.Cu"}, {}, ("F.Cu", "B.Cu")),
            # SGGS: the second ground plane is a zone drawn in the source board.
            "SGGS": ({"GND": "In1.Cu"}, {"In2.Cu": ["GND"]}, ("F.Cu", "B.Cu")),
            "SSGS": ({"GND": "In2.Cu"}, {}, ("F.Cu", "In1.Cu", "B.Cu")),
        }
        for code, (planes, zones, grid) in cases.items():
            with self.subTest(code=code):
                stack = resolve(plane_rules(4, planes), record(code, zones))
                self.assertEqual(stack.grid_layers, grid)
                expected = [
                    (name, "GND" if role == "G" else "VCC")
                    for name, role in zip(copper_names(4), code)
                    if role in "GP"
                ]
                self.assertEqual(list(stack.dedicated), expected)
        stack = resolve(plane_rules(4, {"GND": "In1.Cu"}), record("SGGS", {"In2.Cu": ["GND"]}))
        self.assertEqual(stack.net_planes("GND"), ["In1.Cu", "In2.Cu"])
        self.assertEqual(stack.plane_nets, frozenset({"GND"}))

    def test_six_and_eight_layers(self):
        stack = resolve(
            plane_rules(6, {"GND": "In1.Cu", "VCC": "In4.Cu"}),
            record("SGSGPS", {"In3.Cu": ["GND"]}),
        )
        self.assertEqual(stack.grid_layers, ("F.Cu", "In2.Cu", "B.Cu"))
        self.assertEqual(stack.dedicated, (("In1.Cu", "GND"), ("In3.Cu", "GND"), ("In4.Cu", "VCC")))
        stack = resolve(
            plane_rules(8, {"GND": "In1.Cu", "VCC": "In4.Cu"}),
            record("SGSGPSGS", {"In3.Cu": ["GND"], "In6.Cu": ["GND"]}),
        )
        self.assertEqual(stack.grid_layers, ("F.Cu", "In2.Cu", "In5.Cu", "B.Cu"))
        self.assertEqual(stack.net_planes("GND"), ["In1.Cu", "In3.Cu", "In6.Cu"])
        self.assertEqual(stack.net_planes("VCC"), ["In4.Cu"])

    def test_thirty_two_layers(self):
        code = "S" + "SG" * 15 + "S"
        stack = resolve(plane_rules(32, {}), record(code, {n: ["GND"] for n in copper_names(32)}))
        self.assertEqual(len(stack.layers), 32)
        self.assertEqual(len(stack.dedicated), 15)
        self.assertEqual(stack.grid_layers[0], "F.Cu")
        self.assertEqual(stack.grid_layers[-1], "B.Cu")
        self.assertIn("In29.Cu", stack.grid_layers)
        self.assertEqual(stack.layer("In30.Cu").role, PLANE)

    def test_mixed_jumper_and_split(self):
        rules = plane_rules(4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        stack = resolve(rules, record("SMPS"))
        self.assertEqual(stack.layer("In1.Cu").role, SPLIT)
        self.assertEqual(stack.split_nets, {"GND": "In1.Cu"})
        self.assertEqual(stack.grid_layers, ("F.Cu", "In1.Cu", "B.Cu"))
        self.assertEqual(stack.plane_nets, frozenset({"VCC"}))
        stack = resolve(plane_rules(4, {}), record("SJSS"))
        self.assertEqual(stack.layer("In1.Cu").role, UNUSED)
        self.assertEqual(stack.grid_layers, ("F.Cu", "In2.Cu", "B.Cu"))
        # A signal-typed layer named by a class plane is a split plane (as today).
        stack = resolve(plane_rules(4, {"GND": "In1.Cu", "VCC": "In2.Cu"}), record("SSPS"))
        self.assertEqual(stack.layer("In1.Cu").role, SPLIT)
        self.assertEqual(stack.layer("In2.Cu").role, PLANE)

    def test_outer_power_is_mixed_with_a_warning(self):
        stack = resolve(plane_rules(4, {}), record("PSSS"))
        self.assertEqual(stack.layer("F.Cu").role, SPLIT)
        self.assertEqual(stack.layer("In1.Cu").role, SIGNAL)
        self.assertTrue(stack.warnings)

    def test_errors(self):
        with self.assertRaises(ValueError):
            resolve(plane_rules(4, {"GND": "In5.Cu"}), record("SGPS"))
        with self.assertRaises(ValueError):
            resolve(plane_rules(4, {}), record("SGPS", kinds={"In1.Cu": "bogus"}))


class RungConsistency(unittest.TestCase):
    def test_dedicated_planes_are_the_judges_no_track_layers(self):
        for spec in hard_rungs():
            code = "".join("S" if x["role"] == "signal" else "G" for x in spec["stackup"]["layers"])
            zones = {}
            for layer, net in zone_layers(spec, "extra"):
                zones.setdefault(layer, []).append(net)
            stack = resolve(rules_of(spec), record(code, zones))
            no_tracks = re.findall(r'\(rule "([^ ]+) is a [^ ]+ plane: no tracks"', dru_text(spec))
            with self.subTest(case=spec["name"]):
                self.assertIsNotNone(stack)
                self.assertEqual([layer for layer, _ in stack.dedicated], no_tracks)
                self.assertEqual(
                    list(stack.dedicated),
                    [
                        (x["name"], x["net"])
                        for x in spec["stackup"]["layers"]
                        if x["role"] == "plane"
                    ],
                )


if __name__ == "__main__":
    unittest.main()
