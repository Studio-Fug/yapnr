"""The board's own copper stack (pnr.stack): roles, legacy equality, rung consistency."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))

from designs import designs, showcases  # noqa: E402
from hard_rungs import dru_text, hard_rungs  # noqa: E402
from stackup import stackup_text, zone_layers  # noqa: E402

from pnr.constraints import compile_constraints, compile_routing_rules  # noqa: E402
from pnr.graph import BoardGraph  # noqa: E402
from pnr.stack import (  # noqa: E402
    PLANE,
    SIGNAL,
    SPLIT,
    UNUSED,
    PlaneAccess,
    assess,
    bridge_layer_names,
    contact_layer_names,
    copper_names,
    grid_layers,
    legacy_layers,
    local_record,
    plane_nets,
    plane_regions,
    power_layer_names,
    record_from_rows,
    reference_layer,
    reference_nets,
    resolve,
    si_stackup,
    split_plane_patterns,
    stackup_rows,
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

    def test_rules_naming_a_missing_plane_layer_raise(self):
        with self.assertRaises(ValueError):
            resolve(plane_rules(4, {"GND": "In5.Cu"}), record("SGPS"))

    def test_unusable_stacks_fall_back_to_legacy_with_a_warning(self):
        cases = {
            "unknown type": (plane_rules(4, {}), record("SGPS", kinds={"In1.Cu": "bogus"})),
            "outer jumper": (plane_rules(4, {}), record("JGPS")),
            "one layer": (dict(layers=1), record_from_rows([dict(name="F.Cu")])),
            "layer count": (plane_rules(4, {}), record("SS")),
        }
        for label, (rules, rec) in cases.items():
            with self.subTest(case=label):
                stack, warnings = assess(rules, rec)
                self.assertIsNone(stack)
                self.assertEqual(len(warnings), 1)
                self.assertIn("legacy layer heuristic", warnings[0])
                self.assertEqual(grid_layers(rules, rec), legacy_layers(rules))
        # The warning names the layers and their types.
        _, warnings = assess(plane_rules(4, {}), record("JGPS"))
        self.assertIn("F.Cu jumper", warnings[0])
        self.assertIn("In1.Cu power", warnings[0])

    def test_zones_on_signal_typed_inner_layers_keep_the_legacy_routing(self):
        # A typical KiCad four-layer board: every layer left at the default type
        # signal, ground and supply pours drawn on the inner layers, no plane class.
        rec = record("SSSS", {"In1.Cu": ["GND"], "In2.Cu": ["VCC"]})
        stack, warnings = assess(plane_rules(4, {}), rec)
        self.assertIsNone(stack)
        self.assertIn("In1.Cu, In2.Cu", warnings[0])
        self.assertIn("type each plane layer power", warnings[0])
        self.assertEqual(grid_layers(plane_rules(4, {}), rec), ("F.Cu", "B.Cu"))
        # Pours on the outer layers only (a two-layer habit): the stack applies.
        rec = record("SSSS", {"F.Cu": ["GND"], "B.Cu": ["GND"]})
        self.assertEqual(
            resolve(plane_rules(4, {}), rec).grid_layers, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        )
        # A typed plane layer makes the stack apply; pours on a signal layer are then
        # reported (tracks cross them, the zone refills around them).
        stack, warnings = assess(
            plane_rules(4, {}), record("SSPS", {"In1.Cu": ["GND"], "In2.Cu": ["VCC"]})
        )
        self.assertEqual(stack.grid_layers, ("F.Cu", "In1.Cu", "B.Cu"))
        self.assertEqual(stack.dedicated, (("In2.Cu", "VCC"),))
        self.assertTrue(any("In1.Cu is a signal layer with zones of GND" in w for w in warnings))

    def test_the_boards_no_track_rules_make_a_plane(self):
        # A board that marks its planes only by custom rules (disallow track) on
        # layers typed signal: the stack applies and those layers carry no tracks.
        rec = record("SSSS", {"In1.Cu": ["GND"], "In2.Cu": ["VCC"]})
        rec["no_track_layers"] = ["In1.Cu", "In2.Cu", "F.Cu"]
        stack, warnings = assess(plane_rules(4, {}), rec)
        self.assertEqual(stack.grid_layers, ("F.Cu", "B.Cu"))
        self.assertEqual(stack.dedicated, (("In1.Cu", "GND"), ("In2.Cu", "VCC")))
        self.assertTrue(any("disallow tracks on In1.Cu (typed signal)" in w for w in warnings))
        self.assertTrue(any("outer layer F.Cu" in w for w in warnings))

    def test_no_track_rules_are_read_from_the_judges_rules_file(self):
        import tempfile

        from pnr.ingest import _no_track_layers

        spec = next(r for r in hard_rungs() if r["name"].endswith("8L-SGSGPSGS"))
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.with_suffix(".kicad_dru").write_text(dru_text(spec))
            self.assertEqual(_no_track_layers(str(board)), ["In1.Cu", "In3.Cu", "In4.Cu", "In6.Cu"])
            # A conditional rule about something else does not count.
            board.with_suffix(".kicad_dru").write_text(
                '(version 1)\n(rule "x"\n  (layer "In2.Cu")\n'
                "  (condition \"A.NetClass == 'hv'\")\n  (constraint disallow track))\n"
            )
            self.assertEqual(_no_track_layers(str(board)), [])
        self.assertEqual(_no_track_layers(None), [])

    def test_a_power_layer_without_a_net_warns(self):
        stack, warnings = assess(plane_rules(4, {}), record("SPSS"))
        self.assertEqual(stack.layer("In1.Cu").role, PLANE)
        self.assertEqual(stack.layer("In1.Cu").nets, ())
        self.assertTrue(any("In1.Cu is typed power but no net" in w for w in warnings))
        self.assertEqual(stack.warnings, warnings)


def shape(net, outline, priority=0):
    return dict(net=net, priority=priority, outline=[list(p) for p in outline])


class PlaneRegions(unittest.TestCase):
    W, H = 24.0, 16.0

    def test_one_net_per_full_plane_constrains_nothing(self):
        # Every hard rung: each plane layer one net over the whole outline, from a
        # class or from a full-outline source zone.
        full = [(0, 0), (self.W, 0), (self.W, self.H), (0, self.H)]
        rec = record("SGSGPSGS", {"In3.Cu": ["GND"], "In6.Cu": ["GND"]})
        for row in rec["layers"]:
            if row["name"] in ("In3.Cu", "In6.Cu"):
                row["zone_shapes"] = [shape("GND", full)]
        stack = resolve(plane_rules(8, {"GND": "In1.Cu", "VCC": "In4.Cu"}), rec)
        regions = plane_regions(stack, rec, {"GND": [(1, 1)], "VCC": [(2, 2)]}, self.W, self.H)
        self.assertTrue(all(r.outline is None for r in regions))
        self.assertEqual(
            sorted((r.layer, r.net, r.source) for r in regions),
            [
                ("In1.Cu", "GND", False),
                ("In3.Cu", "GND", True),
                ("In4.Cu", "VCC", False),
                ("In6.Cu", "GND", True),
            ],
        )
        access = PlaneAccess(stack, regions, 0.3, 0.75)
        self.assertFalse(access.constrained("GND"))
        self.assertFalse(access.constrained("VCC"))

    def test_a_shared_plane_layer_splits_by_pad_boxes(self):
        stack = resolve(plane_rules(4, {"GND": "In1.Cu", "VCC": "In1.Cu"}), record("SPSS"))
        pads = {"GND": [(2, 2), (20, 12), (6, 6)], "VCC": [(5, 5), (10, 8)]}
        regions = plane_regions(stack, None, pads, self.W, self.H)
        gnd, vcc = sorted(regions, key=lambda r: r.priority)
        self.assertEqual((gnd.net, gnd.priority, gnd.outline), ("GND", 0, None))
        self.assertEqual(vcc.net, "VCC")
        self.assertEqual(vcc.priority, 1)
        self.assertEqual(vcc.outline, ((3.0, 3.0), (12.0, 3.0), (12.0, 10.0), (3.0, 10.0)))
        access = PlaneAccess(stack, regions, 0.3, 0.75)
        self.assertTrue(access.constrained("GND"))
        self.assertTrue(access.constrained("VCC"))
        # GND fills the outline except VCC's box (which carves out of it), and keeps
        # a via's clearance from that box.
        self.assertTrue(access.site_ok("GND", (20.0, 12.0)))
        self.assertFalse(access.site_ok("GND", (6.0, 6.0)))
        self.assertFalse(access.site_ok("GND", (12.5, 6.0)))
        self.assertTrue(access.site_ok("GND", (13.0, 6.0)))
        # VCC fills only its own box, a via radius inside its edge.
        self.assertTrue(access.site_ok("VCC", (6.0, 6.0)))
        self.assertFalse(access.site_ok("VCC", (11.9, 6.0)))
        self.assertFalse(access.site_ok("VCC", (20.0, 12.0)))

    def test_a_second_plane_lets_a_drop_reach_the_net_elsewhere(self):
        # GND shares In1 with VCC but also has In2 alone: any site reaches GND.
        stack = resolve(
            plane_rules(4, {"GND": "In1.Cu", "VCC": "In1.Cu"}), record("SPPS", {"In2.Cu": ["GND"]})
        )
        regions = plane_regions(
            stack, None, {"GND": [(2, 2), (20, 12), (6, 6)], "VCC": [(5, 5)]}, self.W, self.H
        )
        access = PlaneAccess(stack, regions, 0.3, 0.75)
        self.assertFalse(access.constrained("GND"))
        self.assertTrue(access.constrained("VCC"))

    def test_a_partial_source_zone_bounds_its_drops(self):
        box = [(10, 2), (22, 2), (22, 14), (10, 14)]
        rec = record("SGPS", {"In2.Cu": ["VCC"]})
        rec["layers"][2]["zone_shapes"] = [shape("VCC", box, priority=2)]
        rec = record_from_rows(rec["layers"])  # normalizes and keeps the shapes
        self.assertEqual(rec["layers"][2]["zone_shapes"][0]["priority"], 2)
        stack = resolve(plane_rules(4, {"GND": "In1.Cu"}), rec)
        regions = plane_regions(
            stack, rec, {"GND": [(1, 1)], "VCC": [(3, 3), (15, 8)]}, self.W, self.H
        )
        vcc = [r for r in regions if r.net == "VCC"]
        self.assertEqual(len(vcc), 1)
        self.assertTrue(vcc[0].source)
        self.assertEqual(vcc[0].outline, tuple(map(tuple, map(lambda p: map(float, p), box))))
        access = PlaneAccess(stack, regions, 0.3, 0.75)
        self.assertTrue(access.site_ok("VCC", (15, 8)))
        self.assertFalse(access.site_ok("VCC", (3, 3)))
        self.assertFalse(access.constrained("GND"))
        # A sub-board in a frame of its own drops the shapes: the zone covers it.
        local = local_record(rec)
        self.assertNotIn("zone_shapes", local["layers"][2])
        self.assertEqual(local["layers"][2]["zones"], ["VCC"])
        regions = plane_regions(stack, local, {"VCC": [(3, 3)]}, 5.0, 5.0)
        self.assertTrue(all(r.outline is None for r in regions))

    def test_region_priority_decides_overlaps(self):
        # Two source zones on one power layer: the higher priority wins the overlap.
        a = [(0, 0), (14, 0), (14, 16), (0, 16)]
        b = [(10, 0), (24, 0), (24, 16), (10, 16)]
        rec = record("SPSS", {"In1.Cu": ["GND", "VCC"]})
        rec["layers"][1]["zone_shapes"] = [shape("GND", a, 0), shape("VCC", b, 3)]
        stack = resolve(plane_rules(4, {}), rec)
        access = PlaneAccess(stack, plane_regions(stack, rec, {}, self.W, self.H), 0.3, 0.75)
        self.assertTrue(access.site_ok("VCC", (12, 8)))
        self.assertFalse(access.site_ok("GND", (12, 8)))
        self.assertTrue(access.site_ok("GND", (5, 8)))


class NativeLayers(unittest.TestCase):
    def test_legacy_lists_are_the_native_loops_own(self):
        self.assertEqual(power_layer_names(None), ["F.Cu", "B.Cu", "In2.Cu"])
        self.assertEqual(bridge_layer_names(None), ["B.Cu", "In2.Cu", "F.Cu"])
        self.assertEqual(contact_layer_names(None), ["F.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(reference_layer(None), "In1.Cu")
        self.assertEqual(reference_layer(None, {"reference_layer": "In2.Cu"}), "In2.Cu")
        rules = plane_rules(4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        self.assertEqual(reference_nets(None, rules, "In1.Cu"), {"GND"})

    def test_a_four_layer_ssgs_stack(self):
        # In2 is the ground plane, In1 a routed signal layer: power paths may use
        # In1 (never the plane), and a pair's reference plane is In2.
        rules = plane_rules(4, {"GND": "In2.Cu"})
        stack = resolve(rules, record("SSGS"))
        self.assertEqual(power_layer_names(stack), ["F.Cu", "B.Cu", "In1.Cu"])
        self.assertEqual(bridge_layer_names(stack), ["B.Cu", "In1.Cu", "F.Cu"])
        self.assertEqual(contact_layer_names(stack), ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertEqual(reference_layer(stack), "In2.Cu")
        self.assertEqual(reference_nets(stack, rules, "In2.Cu"), {"GND"})

    def test_plane_layers_never_carry_power_paths(self):
        cases = {
            ("SGPS", (("GND", "In1.Cu"), ("VCC", "In2.Cu"))): ["F.Cu", "B.Cu"],
            ("SGSGPS", (("GND", "In1.Cu"), ("VCC", "In4.Cu"))): ["F.Cu", "B.Cu", "In2.Cu"],
            ("SGSSPS", (("GND", "In1.Cu"), ("VCC", "In4.Cu"))): [
                "F.Cu",
                "B.Cu",
                "In2.Cu",
                "In3.Cu",
            ],
        }
        for (code, planes), expected in cases.items():
            with self.subTest(code=code):
                stack = resolve(plane_rules(len(code), dict(planes)), record(code))
                self.assertEqual(power_layer_names(stack), expected)
                self.assertEqual(reference_layer(stack), "In1.Cu")

    def test_the_reference_plane_is_the_nearest_one(self):
        stack = resolve(plane_rules(6, {"VCC": "In4.Cu"}), record("SSSSPS"))
        self.assertEqual(stack.reference_plane("F.Cu").name, "In4.Cu")
        self.assertEqual(stack.reference_plane("B.Cu").name, "In4.Cu")
        stack = resolve(plane_rules(4, {}), record("SSSS"))
        self.assertIsNone(stack.reference_plane("F.Cu"))
        self.assertEqual(reference_layer(stack), "In1.Cu")


class SignalIntegrityStack(unittest.TestCase):
    def test_the_si_model_takes_the_declared_stackup(self):
        from hard_rungs import stackup as rung_stackup

        from pnr.si import physics

        spec = rung_stackup("8L-SGSGPSGS")
        text = "(kicad_pcb\n\t(setup\n" + stackup_text(spec) + "\n\t)\n)"
        code = "".join("S" if x["role"] == "signal" else "G" for x in spec["layers"])
        rec = record(code, {"In3.Cu": ["GND"], "In6.Cu": ["GND"]})
        stack = resolve(plane_rules(8, {"GND": "In1.Cu", "VCC": "In4.Cu"}), rec)
        st = si_stackup(stackup_rows(text), stack)
        self.assertEqual(st["planes"], ["In1.Cu", "In3.Cu", "In4.Cu", "In6.Cu"])
        model = physics.stackup(dict(stackup=st))
        self.assertEqual(physics.copper_layers(model), list(copper_names(8)))
        dielectric = sum(d["thickness_mm"] for d in spec["dielectrics"])
        self.assertAlmostEqual(physics.thickness(model), 2 * 0.035 + 6 * 0.0152 + dielectric)
        # F.Cu's trace references In1 at 0.09 mm, not the default 4L stack's In1.
        self.assertAlmostEqual(physics.via_span_mm(model, "F.Cu", "In1.Cu"), 0.035 + 0.09)
        # A block that leaves a dielectric's thickness out, or other copper: none.
        rows = stackup_rows(text)
        rows[4] = dict(rows[4], thickness_mm=None)
        self.assertIsNone(si_stackup(rows, stack))
        self.assertIsNone(si_stackup(stackup_rows(text)[:6], stack))


class PlacementPlaneTerms(unittest.TestCase):
    def test_only_split_planes_keep_their_placement_terms(self):
        from types import SimpleNamespace

        from pnr.graph import Net

        graph = BoardGraph(
            name="x", nets=[Net(n, i, []) for i, n in enumerate(("GND", "VCC", "A"))]
        )
        classes = [
            SimpleNamespace(nets=("GND",), plane_layer="In1.Cu"),
            SimpleNamespace(nets=("V*",), plane_layer="In2.Cu"),
        ]
        constraints = SimpleNamespace(board=SimpleNamespace(layers=4), net_classes=classes)
        # No declared stack: the class patterns, as before.
        self.assertEqual(split_plane_patterns(constraints, graph), ["GND", "V*"])
        # Both dedicated planes: no split plane, so no plane placement terms.
        graph.stack = record("SGPS")
        self.assertEqual(split_plane_patterns(constraints, graph), [])
        # A mixed In2: VCC is a split plane there and keeps its terms.
        graph.stack = record("SGMS")
        self.assertEqual(split_plane_patterns(constraints, graph), ["VCC"])
        # Planes from the rules on signal-typed layers: legacy, unchanged.
        graph.stack = record("SSSS")
        self.assertEqual(split_plane_patterns(constraints, graph), ["GND", "V*"])


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
                # A partitioned layer (plane_partition) carries several rails: once.
                layers = list(dict.fromkeys(layer for layer, _ in stack.dedicated))
                self.assertEqual(layers, no_tracks)
                if spec["constraints"].get("plane_partition"):
                    (part,) = spec["constraints"]["plane_partition"]
                    if part.get("region"):
                        # An outer pour (stage 3c E1): its layer stays a signal layer
                        # elsewhere, not a dedicated plane, so it carries no entry here.
                        self.assertNotIn(part["layer"], (layer for layer, _ in stack.dedicated))
                        continue
                    self.assertEqual(
                        [n for layer, n in stack.dedicated if layer == part["layer"]],
                        part["nets"],
                    )
                    continue
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
