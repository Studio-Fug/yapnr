"""Mini failure scenarios: source relations, pad ears, every cap-bank return.

Synthetic fixtures isolate the observed defects; the archived Mini converter
and PD source fixtures separately exercise the real topology. Native checks
run under headless KiCad as well as the ordinary pure-Python test lane.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnr.constraints import BoardSpec, CompiledConstraints, Constraint, Enforcement
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.hier.blocks import extract_blocks
from pnr.hier.partition import partition_violations, validate_partition
from pnr.hier.power_quality import Copper, analyse
from pnr.power_topology import derive


def part(ref, module="converter", xy=(10, 10), pads=()):
    return Component(
        ref,
        "fixture",
        xy,
        0,
        "top",
        (1, 1),
        (1, 1),
        address="board." + module + "." + ref,
        pads=list(pads),
    )


def hard(kind, refs, **params):
    return Constraint(kind=kind, refs=tuple(refs), enforcement=Enforcement.HARD, params=params)


def grouped(fixed=True):
    graph = BoardGraph("fixed-controller", [part(r) for r in ("U5", "C15", "C24")])
    cons = CompiledConstraints(
        board=BoardSpec(30, 30),
        constraints=[
            hard("group", ["C15", "C24"], anchor="U5", radius_mm=5),
        ]
        + ([hard("fixed", ["U5"], at=[10, 10])] if fixed else []),
    )
    return graph, cons


class PartitionScenarios(unittest.TestCase):
    def test_legacy_split_is_caught_before_any_module_routing(self):
        graph, cons = grouped()
        with patch.dict(os.environ, {"PNR_HIER_SAFE_PARTITION": "0"}):
            blocks = extract_blocks(graph, cons)
        self.assertTrue(partition_violations(blocks, cons))
        with self.assertRaisesRegex(ValueError, "before synthesis"):
            validate_partition(blocks, cons)

    def test_fixed_controller_keeps_entire_dependency_flat(self):
        graph, cons = grouped()
        with patch.dict(os.environ, {"PNR_HIER_SAFE_PARTITION": "1"}):
            self.assertEqual(extract_blocks(graph, cons), [])

    def test_movable_controller_stays_with_decouplers(self):
        graph, cons = grouped(False)
        with patch.dict(os.environ, {"PNR_HIER_SAFE_PARTITION": "1"}):
            blocks = extract_blocks(graph, cons)
        self.assertEqual([b.refs for b in blocks], [["C15", "C24", "U5"]])
        validate_partition(blocks, cons)

    def test_cross_module_transitive_anchor_and_unrelated_block(self):
        graph, cons = grouped()
        graph.components += [
            part("R1", "qwiic"),
            part("R2", "qwiic"),
            part("S1", "sensor"),
            part("S2", "sensor"),
        ]
        cons.constraints += [hard("group", ["R1"], anchor="C15", radius_mm=5)]
        with patch.dict(os.environ, {"PNR_HIER_SAFE_PARTITION": "1"}):
            blocks = extract_blocks(graph, cons)
        self.assertEqual([b.refs for b in blocks], [["S1", "S2"]])

    def test_movable_cross_module_relation_merges_whole_blocks(self):
        graph, cons = grouped(False)
        graph.components += [part("R1", "qwiic"), part("R2", "qwiic")]
        cons.constraints += [hard("group", ["R1"], anchor="C15", radius_mm=5)]
        with patch.dict(os.environ, {"PNR_HIER_SAFE_PARTITION": "1"}):
            blocks = extract_blocks(graph, cons)
        self.assertEqual([b.refs for b in blocks], [["C15", "C24", "R1", "R2", "U5"]])
        validate_partition(blocks, cons)


EARS = [
    (2, 0),
    (72, 0),
    (72, 55),
    (2, 55),
    (2, 15.8),
    (0, 15.8),
    (0, 13.8),
    (2, 13.8),
    (2, 10.2),
    (0, 10.2),
    (0, 8.2),
    (2, 8.2),
]


class OutlineScenarios(unittest.TestCase):
    def test_triangle_with_only_bounding_box_corners_is_not_a_rectangle(self):
        from pnr.place.legalize import legalize, legalize_constraint_kwargs
        from pnr.place.metrics import outside_outline
        from pnr.route.detail.grid import RouteGrid

        graph = BoardGraph(
            "triangle",
            [part("C1", xy=(8, 8))],
            outline=BoardOutline(10, 10, [(0, 0), (10, 0), (0, 10)]),
        )
        cons = CompiledConstraints(board=BoardSpec(10, 10))
        self.assertEqual(outside_outline(graph, 10, 10), ["C1"])
        placed = legalize(graph, 10, 10, **legalize_constraint_kwargs(graph, cons, {}))
        self.assertEqual(outside_outline(placed, 10, 10), [])
        grid = RouteGrid.from_graph(graph, 10, 10, pitch=0.1)
        x, y = grid.cell_of(8, 8)
        self.assertTrue(grid.blocked[:, y, x].all())

    def test_channel_refinement_rejects_a_notch_pose(self):
        from pnr.place.channels import ChannelModel
        from pnr.place.legalize import refine_channels

        graph, rules, _ = bank()
        graph.outline = BoardOutline(72, 55, EARS)
        graph.components[0].pos = (3, 12)
        model = ChannelModel(graph, rules)
        with patch.object(
            model, "penalty", side_effect=lambda c, others, x, y: 0 if x < 2 else 10
        ), patch("pnr.place.legalize._place_part", return_value=(45, 1)):
            placed = refine_channels(
                graph, 72, 55, fixed={"C53", "C54"}, keepouts=[], channel_model=model
            )
        self.assertEqual(placed.component("D1").pos, (3, 12))

    def test_routing_grid_cannot_cross_the_notch_between_ears(self):
        from pnr.route.detail.grid import RouteGrid

        graph = BoardGraph("ears", outline=BoardOutline(72, 55, EARS))
        grid = RouteGrid.from_graph(graph, 72, 55, pitch=0.1)
        x, y = grid.cell_of(1, 12)
        self.assertTrue(grid.blocked[:, y, x].all())
        self.assertTrue(grid.via_blocked[:, y, x].all())
        x, y = grid.cell_of(1, 9.2)
        self.assertFalse(grid.blocked[:, y, x].any())

    def test_legalizer_moves_part_from_notch_to_real_board(self):
        from pnr.place.legalize import legalize, legalize_constraint_kwargs
        from pnr.place.metrics import outside_outline

        graph = BoardGraph("ears", [part("C15", xy=(1, 12))], outline=BoardOutline(72, 55, EARS))
        cons = CompiledConstraints(board=BoardSpec(72, 55))
        placed = legalize(graph, 72, 55, **legalize_constraint_kwargs(graph, cons, {}))
        self.assertEqual(outside_outline(placed, 72, 55), [])

    def test_component_between_ears_is_outside_even_inside_bounding_box(self):
        from pnr.place.metrics import outside_outline, part_rules

        graph = BoardGraph(
            "ears",
            [part("bad", xy=(1, 12)), part("good", xy=(1, 9.2))],
            outline=BoardOutline(72, 55, EARS),
        )
        cons = CompiledConstraints(board=BoardSpec(72, 55))
        self.assertEqual(outside_outline(graph, 72, 55), ["bad"])
        self.assertFalse(part_rules(graph, cons)(graph.component("bad")))
        self.assertTrue(part_rules(graph, cons)(graph.component("good")))

    def test_stale_rectangular_constraints_cannot_shrink_tabbed_outline(self):
        from pnr.place.geometry import outline_size

        graph = BoardGraph("ears", outline=BoardOutline(72, 55, EARS))
        with patch.dict(os.environ, {"PNR_NATIVE_OUTLINE": "1"}):
            with self.assertRaisesRegex(ValueError, "outline.*dimensions"):
                outline_size(graph, CompiledConstraints(board=BoardSpec(70, 55)))

    @unittest.skipUnless(importlib.util.find_spec("pcbnew"), "native KiCad required")
    def test_native_pad_ears_preserve_area_and_physical_origin(self):
        import pcbnew as k

        from pnr.ingest import _board_frame

        board = k.BOARD()
        for a, b in zip(EARS, EARS[1:] + EARS[:1]):
            edge = k.PCB_SHAPE(board)
            edge.SetShape(k.SHAPE_T_SEGMENT)
            edge.SetLayer(k.Edge_Cuts)
            for setter, p in ((edge.SetStart, a), (edge.SetEnd, b)):
                setter(k.VECTOR2I(round((p[0] + 28) * 1e6), round((85 - p[1]) * 1e6)))
            edge.SetWidth(150000)
            board.Add(edge)
        with patch.dict(os.environ, {"PNR_NATIVE_OUTLINE": "1"}):
            frame, outline = _board_frame(board)
        poly = outline.polygon
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2
        self.assertAlmostEqual(area, 3858)
        self.assertEqual(set(poly), set(EARS))
        self.assertEqual(frame.point(30000000, 85000000), (2, 0))
        from pnr.writeback import writeback

        with tempfile.TemporaryDirectory() as tmp:
            source, dest = (str(Path(tmp) / name) for name in ("source.kicad_pcb", "out.kicad_pcb"))
            k.SaveBoard(source, board)
            with patch.dict(os.environ, {"PNR_NATIVE_OUTLINE": "1"}):
                writeback(source, BoardGraph("ears", outline=outline), dest, width=72, height=55)
                _, saved = _board_frame(k.LoadBoard(dest))
                self.assertEqual(set(saved.polygon), set(EARS))
                rectangle = BoardOutline(72, 55, [(0, 0), (72, 0), (72, 55), (0, 55)])
                with self.assertRaisesRegex(ValueError, "contours differ"):
                    writeback(
                        source, BoardGraph("wrong", outline=rectangle), dest, width=72, height=55
                    )
                with self.assertRaisesRegex(ValueError, "outline cannot be preserved"):
                    writeback(
                        source, BoardGraph("ears", outline=outline), dest, width=70, height=55
                    )


def bank():
    parts = [
        part(r, "pd", (x, 2), [Pad("1", "VIN", (0, -1)), Pad("2", "GND", (0, 1))])
        for r, x in (("D1", 1), ("C53", 3), ("C54", 5))
    ]
    graph = BoardGraph(
        "bank",
        parts,
        [Net(n, i + 1, [(c.ref, str(i + 1)) for c in parts]) for i, n in enumerate(("VIN", "GND"))],
    )
    rules = dict(
        fab=dict(track_width_mm=0.2),
        electrical_nets={"VIN": dict(rms_current_a=5, peak_current_a=5, outer_width_mm=1.5)},
        net_classes=[dict(name="return", nets=["GND"], plane_layer="In1.Cu")],
    )
    dump = dict(copper_layers=["F.Cu", "In1.Cu", "B.Cu"], pads=[], tracks=[], vias=[], zones=[])
    for c in parts:
        for p in c.pads:
            x, y = c.pos[0], c.pos[1] + p.offset[1]
            dump["pads"].append(
                dict(
                    ref=c.ref,
                    name=p.name,
                    net=p.net,
                    x=x,
                    y=y,
                    bbox=[x - 0.2, y - 0.2, x + 0.2, y + 0.2],
                    layers=["F.Cu"],
                    smd=True,
                    zone_hits=[],
                )
            )
    for net, y in (("VIN", 1), ("GND", 3)):
        dump["tracks"].append(dict(net=net, layer="F.Cu", x1=1, y1=y, x2=5, y2=y, w=1.5, length=4))
    return graph, rules, dump


class PowerScenarios(unittest.TestCase):
    def test_branch_screen_alone_preserves_the_worst_repeat(self):
        from pnr.hier.synth_native import aggregate, is_complete

        good = dict(status="ok", objective=[0] * 6, hot_loops_open=0, power_loop_failures=0)
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1", "PNR_POWER_FIRST": "0"}):
            for failures in (None, 2):
                result = aggregate([good, dict(good, power_loop_failures=failures)])
                self.assertEqual(result["power_loop_failures"], failures)
                self.assertFalse(is_complete(result))

    def test_no_power_is_distinct_from_an_unknown_power_model(self):
        from pnr.hier.power_coverage import check
        from pnr.power_loop import screen

        roles = dict(power_nets=[], return_nets=[], carrying={}, classes=[], loops=[])
        report = check(roles, [], connectivity_valid=True)
        self.assertEqual(report["status"], "not_applicable")
        self.assertEqual(screen({}, roles, {})["failure_count"], 0)
        unknown = dict(roles, power_nets=["VIN"])
        self.assertFalse(check(unknown, [], connectivity_valid=True)["coverage_complete"])
        self.assertIsNone(screen({}, unknown, {})["failure_count"])

    def test_missing_native_drc_cannot_become_a_zero_failure_result(self):
        from pnr.hier.power_quality import evaluate

        graph, rules, dump = bank()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PNR_POWER_BRANCHES": "1"}
        ), patch("pnr.power_loop.evaluate", return_value={"failure_count": 0}):
            result = evaluate(tmp, graph, rules, dump=dump)
        self.assertFalse(result["valid"])
        self.assertIsNone(result["hot_loops_open"])
        self.assertFalse(result["coverage"]["coverage_complete"])

    @unittest.skipUnless(importlib.util.find_spec("pcbnew"), "native KiCad required")
    def test_native_off_centre_pad_contacts_keep_fill_islands_separate(self):
        import pcbnew as k

        from pnr.hier.power_quality import dump_main

        board = k.BOARD()
        net = k.NETINFO_ITEM(board, "GND")
        board.Add(net)
        for i, x in enumerate((10, 20, 30)):
            fp = k.FOOTPRINT(board)
            fp.SetReference("C%d" % i)
            board.Add(fp)
            pad = k.PAD(fp)
            pad.SetNumber("1")
            pad.SetAttribute(k.PAD_ATTRIB_PTH)
            pad.SetShape(k.PAD_SHAPE_CIRCLE)
            pad.SetSize(k.VECTOR2I(2000000, 2000000))
            pad.SetDrillSize(k.VECTOR2I(800000, 800000))
            pad.SetLayerSet(k.LSET.AllCuMask())
            pad.SetPosition(k.VECTOR2I(x * 1000000, 10000000))
            pad.SetNet(net)
            fp.Add(pad)
        zone = k.ZONE(board)
        zone.SetNet(net)
        zone.SetLayer(k.F_Cu)
        board.Add(zone)
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in ((8, 8), (32, 8), (32, 12), (8, 12)):
            outline.Append(k.VECTOR2I(x * 1000000, y * 1000000))
        fill = k.SHAPE_POLY_SET()
        for x in (10, 20):
            fill.NewOutline()
            for dx, y in ((-1, 10.2), (1, 10.2), (1, 11), (-1, 11)):
                fill.Append(k.VECTOR2I(round((x + dx) * 1e6), round(y * 1e6)))
        zone.SetFilledPolysList(k.F_Cu, fill)
        zone.SetIsFilled(True)
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = (Path(tmp) / n for n in ("native.kicad_pcb", "copper.json"))
            k.SaveBoard(str(src), board)
            dump_main(src, dst)
            dumped = json.loads(dst.read_text())
        self.assertEqual(dumped["contact_errors"], [])
        pads = {p["ref"]: p for p in dumped["pads"]}
        self.assertEqual(len(pads["C0"]["zone_hits"]), 1)
        self.assertEqual(len(pads["C1"]["zone_hits"]), 1)
        self.assertNotEqual(pads["C0"]["zone_hits"], pads["C1"]["zone_hits"])
        self.assertEqual(pads["C2"]["zone_hits"], [])
        self.assertEqual(Copper(dumped, "GND").islands(), 3)

    def test_connected_narrow_return_fails_copper_screen_and_wide_control_passes(self):
        from pnr.power_loop import screen

        graph, rules, _ = bank()
        rules["electrical_fab"] = {"min_via_plating_um": 18, "delta_t_c": 40}
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1"}):
            roles = derive(graph, None, rules)

        def coppers(width):
            out = {}
            for net, pad in (("VIN", "1"), ("GND", "2")):
                pads = []
                for ref, x in (("D1", 0), ("C53", 5), ("C54", 10)):
                    pads.append(
                        dict(
                            ref=ref,
                            pad=pad,
                            at=[x, 0],
                            layers=["F.Cu"],
                            polygon=[
                                [x - 0.01, -0.01],
                                [x + 0.01, -0.01],
                                [x + 0.01, 0.01],
                                [x - 0.01, 0.01],
                            ],
                        )
                    )
                out[net] = dict(
                    net=net,
                    layers=[dict(name="F.Cu", z_mm=0, copper_mm=0.035)],
                    pads=pads,
                    zones=[],
                    arcs=[],
                    vias=[],
                    tracks=[
                        dict(layer="F.Cu", a=[0, 0], b=[5, 0], width_mm=10),
                        dict(
                            layer="F.Cu",
                            a=[5, 0],
                            b=[10, 0],
                            width_mm=width if net == "GND" else 10,
                        ),
                    ],
                )
            return out

        bad = screen(coppers(0.03), roles, rules)
        good = screen(coppers(10), roles, rules)
        self.assertGreater(bad["failure_count"], 0)
        self.assertTrue(any(r["net"] == "GND" and r["necks"] for r in bad["links"]))
        neck_warnings = []
        for link in bad["links"]:
            self.assertEqual(link["report"]["copper_delta_t_c"], 40)
            for finding in link["report"].get("warnings", []):
                if "local neck" in finding.get("consequence", ""):
                    neck_warnings.append(finding)
                    self.assertIn("40 C rise", finding["consequence"])
        self.assertTrue(neck_warnings)
        self.assertEqual(good["failure_count"], 0)
        self.assertFalse(good["qualified"])

    def test_missing_return_model_and_other_electrical_failures_cannot_complete(self):
        from pnr.hier.synth_native import is_complete, rank_key

        good = dict(status="ok", objective=[0] * 6, hot_loops_open=0, power_loop_failures=0)
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1", "PNR_POWER_FIRST": "1"}):
            self.assertTrue(is_complete(good))
            for count in (None, 1):
                bad = dict(good, power_loop_failures=count)
                self.assertFalse(is_complete(bad))
                self.assertLess(rank_key(good), rank_key(bad))
            for idx in range(6):
                objective = [0] * 6
                objective[idx] = 1
                self.assertFalse(is_complete(dict(good, objective=objective)))

    def test_real_converter_and_pd_every_parallel_branch_has_coverage(self):
        from power_topology_golden import compile_fixture

        from pnr.hier.power_coverage import check

        folder = Path(__file__).resolve().parents[1] / "testdata" / "power_topology"
        for name in ("converter", "pd"):
            graph, cons, rules = compile_fixture(
                json.loads((folder / (name + ".json")).read_text())
            )
            with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1"}):
                roles = derive(graph, cons, rules)
            measured = []
            for loop in roles["loops"]:
                if not loop["hot"]:
                    continue
                measured.append(
                    dict(
                        links=[
                            dict(
                                net=link["net"],
                                open=False,
                                a=roles["classes"][link["a"]]["members"],
                                b=roles["classes"][link["b"]]["members"],
                            )
                            for link in loop["links"]
                        ]
                    )
                )
            coverage = check(roles, measured, connectivity_valid=True)
            self.assertTrue(coverage["coverage_complete"], (name, coverage))
            self.assertFalse(coverage["qualified"])

    def test_equal_budget_placement_improves_every_branch_without_losing_legality(self):
        import torch
        from power_topology_golden import compile_fixture

        from pnr.place.placer import place
        from pnr.place.power_first import placement_quality

        folder = Path(__file__).resolve().parents[1] / "testdata" / "power_topology"
        old_threads = torch.get_num_threads()
        torch.set_num_threads(1)
        try:
            for name in ("pd", "converter"):
                fixture = json.loads((folder / (name + ".json")).read_text())
                for seed in (0, 1):
                    scores = []
                    for branches in ("0", "1"):
                        graph, cons, rules = compile_fixture(fixture)
                        with patch.dict(
                            os.environ, {"PNR_POWER_FIRST": "1", "PNR_POWER_BRANCHES": branches}
                        ):
                            placed, report = place(
                                graph, cons, seed=seed, iters=120, channel_rules=rules
                            )
                        self.assertTrue(report.legal, (name, seed, branches))
                        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1"}):
                            roles = derive(graph, cons, rules)
                        scores.append(placement_quality(placed, roles)["q_place"])
                    self.assertLess(scores[1], scores[0], (name, seed, scores))
        finally:
            torch.set_num_threads(old_threads)

    def test_pd_parallel_class_erasure_is_detected_independently(self):
        from pnr.hier.power_coverage import check

        graph, rules, _ = bank()
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "0"}):
            roles = derive(graph, None, rules)
        report = check(roles, [], connectivity_valid=True)
        self.assertEqual(len(report["missing_terminals"]), 6)
        self.assertFalse(report["connectivity_pass"])

    def test_every_bank_branch_measured_but_connectivity_does_not_qualify_loop(self):
        graph, rules, dump = bank()
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1"}):
            roles = derive(graph, None, rules)
        result = analyse(dump, roles, rules, {"unconnected_items": []})
        self.assertTrue(result["coverage"]["coverage_complete"])
        self.assertTrue(result["coverage"]["connectivity_pass"])
        self.assertFalse(result["coverage"]["qualified"])
        self.assertGreaterEqual(result["hot_loop_links"], 4)

    def test_open_second_cap_return_cannot_hide_behind_good_first_cap(self):
        graph, rules, dump = bank()
        dump["tracks"][1].update(x2=3, length=2)
        with patch.dict(os.environ, {"PNR_POWER_BRANCHES": "1"}):
            roles = derive(graph, None, rules)
        drc = {"unconnected_items": [{"items": [{"description": "Pad [GND]"}]}]}
        result = analyse(dump, roles, rules, drc)
        self.assertTrue(result["valid"])
        self.assertIn(["C54", "GND"], result["coverage"]["disconnected_terminals"])
        self.assertFalse(result["coverage"]["connectivity_pass"])

    def test_through_hole_is_not_connected_to_every_zone_on_its_layer(self):
        _, _, dump = bank()
        dump["tracks"] = []
        for p in dump["pads"]:
            p.update(smd=False, layers=["F.Cu", "In1.Cu", "B.Cu"])
        dump["zones"] = [dict(net="GND", index=0, layer="In1.Cu", filled_area_mm2=100)]
        self.assertEqual(Copper(dump, "GND").islands(), 3)


class CongestionScenarios(unittest.TestCase):
    def test_shared_power_net_does_not_prove_a_usable_pour(self):
        from pnr.place.channels import ChannelModel
        from pnr.place.power_first import PourChannels

        path = Path(__file__).resolve().parents[1] / "testdata/mini_failures/c53.json"
        fixture = json.loads(path.read_text())
        graph = BoardGraph.from_json(json.dumps(fixture["graph"]))
        base = ChannelModel(graph, fixture["rules"], layers=False)
        model = PourChannels(base, derive(graph, None, fixture["rules"]))
        a, b = [graph.component(r) for r in fixture["observed_pair"]]
        with patch.dict(os.environ, {"PNR_POWER_CHANNELS": "0"}):
            self.assertEqual(float(model.penalty(a, [b], *a.pos)), 0)
        with patch.dict(os.environ, {"PNR_POWER_CHANNELS": "1"}):
            self.assertAlmostEqual(float(model.penalty(a, [b], *a.pos)), 1.21)

    def test_checkpoint_channel_refinement_improves_both_saved_geometries(self):
        from pnr.place.channels import ChannelModel
        from pnr.place.legalize import refine_channels
        from pnr.place.metrics import outside_outline, overlap_pairs

        folder = Path(__file__).resolve().parents[1] / "testdata" / "mini_failures"
        for name in ("c15", "c53"):
            fixture = json.loads((folder / (name + ".json")).read_text())
            graph = BoardGraph.from_json(json.dumps(fixture["graph"]))
            original = graph.to_json()
            model = ChannelModel(graph, fixture["rules"], layers=False)
            placed = refine_channels(
                graph,
                graph.outline.width,
                graph.outline.height,
                fixed=set(),
                keepouts=[],
                channel_model=model,
            )
            self.assertLess(
                model.report(placed)["shortage_score"], model.report(graph)["shortage_score"]
            )
            self.assertEqual(outside_outline(placed, graph.outline.width, graph.outline.height), [])
            self.assertLessEqual(set(overlap_pairs(placed)), set(overlap_pairs(graph)))
            self.assertEqual(graph.to_json(), original)

    def test_c15_and_c53_original_channels_and_engine_pose_order(self):
        from pnr.native_loop import rank_translation_channels
        from pnr.place.channels import ChannelModel

        folder = Path(__file__).resolve().parents[1] / "testdata" / "mini_failures"
        for name, gap in (("c15", 0.757), ("c53", 0.7)):
            d = json.loads((folder / (name + ".json")).read_text())
            graph = BoardGraph.from_json(json.dumps(d["graph"]))
            original = graph.to_json()
            report = ChannelModel(graph, d["rules"], layers=False).report(graph)
            channel = next(
                c for c in report["channels"] if set(c["refs"]) == set(d["observed_pair"])
            )
            self.assertAlmostEqual(channel["gap_mm"], gap)
            self.assertAlmostEqual(channel["required_mm"], 1.8)
            self.assertGreater(channel["shortage_mm"], 1)
            poses = [
                dict(ref=name.upper(), dx=x, dy=y, rank=i, score=0)
                for i, (x, y) in enumerate([(0, 0.5), (-0.5, 0), (0.5, 0), (0, -0.5)])
            ]
            with patch.dict(os.environ, {"PNR_CHANNEL_LAYERS": "0"}):
                ranked = rank_translation_channels(graph, d["rules"], poses)
            self.assertGreater(ranked[0]["channel_improvement"], 0)
            self.assertEqual((ranked[0]["dx"], ranked[0]["dy"]), (0, -0.5))
            self.assertEqual(graph.to_json(), original)

    def test_repair_flags_cannot_reuse_legacy_feedback(self):
        from pnr.feedback.signals import check_import, current_key, key_string

        flags = {
            "PNR_HIER_SAFE_PARTITION": "0",
            "PNR_NATIVE_OUTLINE": "0",
            "PNR_POWER_BRANCHES": "0",
        }
        with patch.dict(os.environ, flags):
            old = current_key("block", 300)
        with patch.dict(os.environ, dict(flags, PNR_POWER_BRANCHES="1")):
            new = current_key("block", 300)
        self.assertNotEqual(key_string(old), key_string(new))
        self.assertTrue(any("repairs" in x for x in check_import(old, new)[0]))

    def test_separate_filled_islands_are_not_a_common_plane(self):
        _, _, dump = bank()
        dump["tracks"] = []
        for i, p in enumerate(p for p in dump["pads"] if p["net"] == "GND"):
            p["zone_hits"] = [[0, "F.Cu", i]]
        self.assertEqual(Copper(dump, "GND").islands(), 3)


if __name__ == "__main__":
    unittest.main()
