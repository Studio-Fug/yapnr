"""Independent assertions for circuit intent and the fail-closed acceptance gate."""

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from designs import LIB as LIBRARY
from designs import PAD_AXIS, designs, showcases
from run import (
    LISTING,
    acceptance,
    case_fab_profile,
    constraint_reasons,
    copper_sha,
    engine_revision,
    freeze_gloss_groups,
    gloss_flags,
    gloss_gate,
    gloss_stage,
    gloss_summary,
    live_emit,
    measure_summary,
    new_result,
    parser,
    scrubbed_suite_env,
    sources_digest,
)


class CircuitContract(unittest.TestCase):
    def test_ladder_and_multiterminal_networks(self):
        cases = designs()
        self.assertEqual([len(c["parts"]) for c in cases], [2, 3, 5, 8, 10, 14, 20, 20])
        self.assertEqual([c["constraints"]["board"]["layers"] for c in cases], [2] * 7 + [4])
        for c in cases:
            with self.subTest(c=c["name"]):
                self.assertEqual(len({p["ref"] for p in c["parts"]}), len(c["parts"]))
                counts = Counter(n for p in c["parts"] for n in p["pins"].values() if n)
                self.assertTrue(all(v >= 2 for v in counts.values()), counts)
                self.assertEqual(set(c["constraints"]["fixed"]), {"J1"})

    def test_timer_and_counter_pin_contract(self):
        for c in designs()[4:]:
            parts = {p["ref"]: p for p in c["parts"]}
            timer = parts["U1"]["pins"]
            self.assertEqual(
                timer,
                {
                    "1": "GND",
                    "2": "TIMING",
                    "3": "CLOCK",
                    "4": "VCC",
                    "5": "CONTROL",
                    "6": "TIMING",
                    "7": "DISCHARGE",
                    "8": "VCC",
                },
            )
            if "U2" in parts:
                counter = parts["U2"]["pins"]
                self.assertEqual(
                    [counter[str(i)] for i in (8, 13, 14, 15, 16)],
                    ["GND", "GND", "CLOCK", "RESET", "VCC"],
                )
                self.assertEqual(counter["4" if len(parts) == 14 else "1"], "RESET")
                self.assertEqual(parts["C4"]["pins"], {"1": "VCC", "2": "GND"})

    def test_no_onboard_current_limiter_only_for_external_current_source(self):
        c = designs()[0]
        self.assertIn("current source", c["description"])
        for c in designs()[1:]:
            self.assertTrue(any(p["ref"].startswith("R") for p in c["parts"]))


class ShowcaseContract(unittest.TestCase):
    """The showcases (run.py --showcases) sit beside the ladder and leave it unchanged."""

    def test_the_ladder_designs_are_unchanged(self):
        digest = hashlib.sha256(json.dumps(designs(), sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, "da91e07a7174377f83191f272edf741f1dd6a7e0f463921c6f0327915e608e21")

    def test_names_stay_out_of_the_ladder(self):
        cases = showcases()
        names = [c["name"] for c in cases]
        self.assertEqual(
            names, ["line-chaser-20", "edge-io-12-free", "edge-io-12", "hier-twin-bank-32"]
        )
        self.assertFalse(set(names) & {c["name"] for c in designs()})
        for c in cases:
            self.assertIsNone(re.match(r"^\d\d-", c["name"]))
            self.assertEqual(int(re.search(r"-(\d+)(-|$)", c["name"]).group(1)), len(c["parts"]))

    def test_pins_are_consistent(self):
        for c in showcases():
            with self.subTest(c=c["name"]):
                self.assertEqual(len({p["ref"] for p in c["parts"]}), len(c["parts"]))
                counts = Counter(n for p in c["parts"] for n in p["pins"].values() if n)
                self.assertTrue(all(v >= 2 for v in counts.values()), counts)
                self.assertEqual(c["expected_components"], len(c["parts"]))

    def test_the_line_group_is_the_only_difference_from_the_ladder_chaser(self):
        from pnr.constraints import compile_constraints

        line, chaser = showcases()[0], designs()[6]
        self.assertEqual(chaser["name"], "07-chaser-20")
        self.assertEqual(line["parts"], chaser["parts"])
        group = line["constraints"].pop("line_group")
        self.assertEqual(line["constraints"], chaser["constraints"])
        refs = {p["ref"] for p in line["parts"]}
        self.assertEqual(group[0]["members"], ["D1", "D2", "D3", "D4", "D5"])
        self.assertTrue(set(group[0]["members"]) <= refs)
        line["constraints"]["line_group"] = group
        compiled = compile_constraints(line["constraints"], sorted(refs))
        self.assertEqual([c.kind for c in compiled.constraints][-1], "line_group")

    def test_edge_orientations_follow_the_pad_axis_table(self):
        """The edge showcase turns J1, SW1 and D1 so that PAD_AXIS's row runs along the
        south edge; test_pad_axis_table_matches_the_footprints checks the table itself."""
        free, edge = showcases()[1:3]
        self.assertEqual(free["parts"], edge["parts"])
        for c in (free, edge):
            self.assertNotIn("fixed", c["constraints"])
        self.assertFalse({"edge_align", "orientation"} & set(free["constraints"]))
        kinds = {
            p["ref"]: k for p in edge["parts"] for k, f in LIBRARY.items() if f == p["footprint"]
        }
        rules = edge["constraints"]["edge_align"]
        self.assertEqual(set(rules), {"J1", "SW1", "D1"})
        for ref, rule in rules.items():
            self.assertEqual((rule["edge"], rule["hard"]), ("south", True))
            turned = int(edge["constraints"]["orientation"][ref]) // 90 % 2
            axis = PAD_AXIS[kinds[ref]]
            along = {"x": "y", "y": "x"}[axis] if turned else axis
            self.assertEqual(along, "x", ref)  # the pad row runs along the south edge

    def test_pad_axis_table_matches_the_footprints(self):
        """PAD_AXIS against the KiCad footprint files themselves: the axis along which the
        pad centres spread, which is also the courtyard's long axis. Reads the
        .kicad_mod text only; skipped where the footprint library is not installed."""
        from run import kicad_footprints

        library = kicad_footprints()
        if not library.is_dir():
            self.skipTest("no KiCad footprint library at %s" % library)
        number = r"(-?[0-9.]+)"
        for kind, axis in PAD_AXIS.items():
            with self.subTest(kind=kind):
                lib, name = LIBRARY[kind].split(":")
                text = (library / (lib + ".pretty") / (name + ".kicad_mod")).read_text()
                items = re.split(r"\n\t\(", text)
                pads = [
                    tuple(float(v) for v in m.groups())
                    for item in items
                    if item.startswith("pad ")
                    for m in [re.search(r"\(at %s %s" % (number, number), item)]
                ]
                court = [
                    tuple(float(v) for v in m.groups())
                    for item in items
                    if '(layer "F.CrtYd")' in item
                    for m in re.finditer(r"\((?:start|end) %s %s\)" % (number, number), item)
                ]
                self.assertGreaterEqual(len(pads), 2)
                self.assertGreaterEqual(len(court), 2)

                def spread(points, i):
                    return max(p[i] for p in points) - min(p[i] for p in points)

                pad_axis = "x" if spread(pads, 0) > spread(pads, 1) else "y"
                court_axis = "x" if spread(court, 0) > spread(court, 1) else "y"
                self.assertEqual((pad_axis, court_axis), (axis, axis))

    def test_twin_banks_share_a_template(self):
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardGraph, Component, Net, Pad
        from pnr.hier.blocks import extract_blocks

        spec = showcases()[3]
        self.assertEqual(spec["driver"], "hier")
        comps, nets = [], {}
        for p in spec["parts"]:
            pads = [Pad(pin, net, (0.0, 0.0), (0.5, 0.5)) for pin, net in p["pins"].items()]
            comps.append(
                Component(
                    p["ref"],
                    p["footprint"],
                    (0.0, 0.0),
                    0.0,
                    "top",
                    (1, 1),
                    (1, 1),
                    pads=pads,
                    address=p["address"],
                )
            )
            for pin, net in p["pins"].items():
                if net:
                    nets.setdefault(net, []).append((p["ref"], pin))
        graph = BoardGraph(
            "pins", comps, [Net(n, i, pins) for i, (n, pins) in enumerate(sorted(nets.items()))]
        )
        compiled = compile_constraints(spec["constraints"], graph.refs)
        blocks = {b.name: b for b in extract_blocks(graph, compiled)}
        self.assertEqual(set(blocks), {"top.clock", "top.bank_a", "top.bank_b"})
        self.assertEqual(blocks["top.bank_a"].template, blocks["top.bank_b"].template)
        self.assertNotEqual(blocks["top.bank_a"].template, blocks["top.clock"].template)
        for b in blocks.values():
            self.assertEqual(b.external_nets, ["CLOCK", "GND", "VCC"])

    def test_runner_options(self):
        args = parser().parse_args(
            ["--out", "x", "--showcases", "--trace", "--trace-placement-every", "5"]
        )
        self.assertEqual((args.showcases, args.trace_placement_every), (True, 5))
        plain = parser().parse_args(["--out", "x"])
        self.assertEqual((plain.showcases, plain.trace_placement_every), (False, None))

    def test_constraint_audit(self):
        line = showcases()[0]
        comps = [
            dict(
                ref="D%d" % (i + 1),
                pos=[10.0, 4.0 + 3.0 * i],
                rot=180.0,
                side="top",
                courtyard=[3.49, 2.04],
            )
            for i in range(5)
        ]
        placed = dict(components=comps)
        checked, findings = constraint_reasons(line, placed)
        self.assertEqual((checked, findings), (["line_group chaser_leds"], []))
        comps[3]["pos"] = [10.2, 13.0]
        self.assertEqual(len(constraint_reasons(line, placed)[1]), 2)
        comps[3]["pos"] = [10.0, 13.0]
        comps[4]["rot"] = 0.0
        self.assertIn("D5", constraint_reasons(line, placed)[1][0])
        edge = showcases()[2]
        comps = [
            dict(ref="J1", pos=[10.0, 1.9], rot=90.0, side="top", courtyard=[3.63, 8.73]),
            dict(ref="SW1", pos=[20.0, 2.5], rot=0.0, side="top", courtyard=[7.9, 4.0]),
            dict(ref="D1", pos=[30.0, 2.3], rot=0.0, side="top", courtyard=[3.49, 2.04]),
        ]
        checked, findings = constraint_reasons(edge, dict(components=comps))
        self.assertEqual(len(checked), 3)
        self.assertEqual(findings, ["D1: 1.280 mm from the south edge"])


class GateContract(unittest.TestCase):
    def setUp(self):
        self.p = dict(legal=True, converged=True, unrouted=[], deferred=[])
        self.a = dict(
            netlist_preserved=True, subwidth_tracks=[], pad_entries=[dict(qualified=True)]
        )
        self.d = dict(unconnected_items=[], violations=[])

    def test_clean(self):
        self.assertEqual(acceptance(self.p, self.a, self.d), [])

    def test_no_native_report_no_pass(self):
        self.assertIn("invalid_drc_report", acceptance(self.p, self.a, {}))

    def test_native_opens_override_router_success(self):
        self.d["unconnected_items"] = [{}]
        self.assertIn("native_unconnected_items", acceptance(self.p, self.a, self.d))

    def test_designed_opens_are_not_reasons(self):
        def item(pad, ref):
            ends = [dict(description="Track [VCC] on F.Cu, length 0.46 mm")]
            ends.append(dict(description="Pad %s [VCC] of %s on F.Cu" % (pad, ref)))
            return dict(type="unconnected_items", items=ends)

        self.d["unconnected_items"] = [item("K4", "U1")]
        self.assertEqual(acceptance(self.p, self.a, self.d, ["U1.K4"]), [])
        self.assertIn("native_unconnected_items", acceptance(self.p, self.a, self.d))
        self.d["unconnected_items"].append(item("K5", "U1"))
        self.assertIn("native_unconnected_items", acceptance(self.p, self.a, self.d, ["U1.K4"]))
        self.d["unconnected_items"] = [item("K4", "U10")]
        self.assertIn("native_unconnected_items", acceptance(self.p, self.a, self.d, ["U1.K4"]))

    def test_every_native_warning_rejected(self):
        for kind in [
            "shorting_items",
            "clearance",
            "track_dangling",
            "via_dangling",
            "lib_footprint_mismatch",
        ]:
            self.d["violations"] = [dict(type=kind, severity="warning")]
            self.assertIn("native_drc_violations", acceptance(self.p, self.a, self.d))

    def test_missing_net_not_hidden_by_zero_opens(self):
        self.a["netlist_preserved"] = False
        self.assertIn("changed_pin_netlist", acceptance(self.p, self.a, self.d))

    def test_narrow_grazing_contact_rejected(self):
        self.a["pad_entries"][0]["qualified"] = False
        self.assertIn("unqualified_pad_entry", acceptance(self.p, self.a, self.d))

    def test_power_or_pair_deferral_is_incomplete(self):
        self.p["deferred"] = ["VCC"]
        self.assertIn("incomplete_pnr", acceptance(self.p, self.a, self.d))

    def test_undersized_copper_rejected(self):
        self.a["subwidth_tracks"] = ["track"]
        self.assertIn("undersized_copper", acceptance(self.p, self.a, self.d))


BOARD = """(kicad_pcb (version 20260206)
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (gr_rect (start 10 20) (end 28 34) (layer "Edge.Cuts"))
  (footprint "LED_SMD:LED_0805_2012Metric" (layer "F.Cu") (at 20 27 90)
    (property "Reference" "D1") (property "Value" "red")
    (pad "1" smd roundrect (at -0.9375 0 90) (size 0.975 1.4) (layers "F.Cu") (roundrect_rratio 0.25)
      (net "GND"))
    (pad "2" smd roundrect (at 0.9375 0 90) (size 0.975 1.4) (layers "F.Cu") (roundrect_rratio 0.25)
      (net "LED_A")))
  (segment (start 20 26) (end 24 26) (width 0.25) (layer "F.Cu") (net "LED_A")))
"""


class RunnerContract(unittest.TestCase):
    def test_trace_is_opt_in(self):
        self.assertFalse(parser().parse_args(["--out", "x"]).trace)
        self.assertTrue(parser().parse_args(["--out", "x", "--trace"]).trace)

    def test_version_listing_needs_no_pip(self):
        out = subprocess.run(
            [sys.executable, "-c", LISTING], capture_output=True, text=True, timeout=120, check=True
        )
        lines = out.stdout.splitlines()
        self.assertTrue(all(re.match(r"^[^=\s]+==\S+$", line) for line in lines), lines[:3])

    def test_the_ladder_is_judged_under_the_fixtures_own_rules_by_default(self):
        self.assertEqual(parser().parse_args(["--out", "x"]).fab_profile, "legacy")
        self.assertEqual(
            parser().parse_args(["--out", "x", "--fab-profile", "jlc-pofv"]).fab_profile, "jlc-pofv"
        )
        with self.assertRaises(SystemExit):
            parser().parse_args(["--out", "x", "--fab-profile", "other"])

    def test_a_case_that_declares_a_fab_profile_is_held_to_it(self):
        bga = dict(name="bga", fab_profile="jlc-6l-hdi")
        self.assertEqual(case_fab_profile(bga, "jlc-pofv"), "jlc-6l-hdi")
        self.assertEqual(case_fab_profile(bga, "oshpark-4l"), "jlc-6l-hdi")
        # legacy: every design's own fab block, the A/B baseline, declaration or not
        self.assertEqual(case_fab_profile(bga, "legacy"), "legacy")
        self.assertEqual(case_fab_profile(dict(name="plain"), "jlc-pofv"), "jlc-pofv")

    def test_route_case_routes_under_the_profile_it_is_judged_by(self):
        source = (Path(__file__).resolve().parent / "route_case.py").read_text()
        # layout-independent (black spaces the assignment)
        self.assertIn("rules=apply_rules(compile_routing_rules(", "".join(source.split()))

    def test_sources_digest_and_revision(self):
        a = sources_digest({"b.py": "2", "a.py": "1"})
        self.assertEqual(a, sources_digest({"a.py": "1", "b.py": "2"}))
        self.assertNotEqual(a, sources_digest({"a.py": "1", "b.py": "3"}))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(engine_revision(Path(tmp) / "not-a-checkout"), (None, None))

    def test_revision_of_a_source_bundle_comes_from_the_task_wrapper(self):
        # yapnr exp runs the ladder from a git archive (no .git); the wrapper names the commit.
        env = {"YAPNR_ENGINE_REVISION": "abc123", "YAPNR_ENGINE_DIRTY": "1"}
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict("os.environ", env):
            self.assertEqual(engine_revision(Path(tmp)), ("abc123", True))

    def test_case_directories_are_run_relative(self):
        spec = designs()[0]
        result = new_result(spec, 1, Path("somewhere/run") / (spec["name"] + "-seed-1"))
        self.assertEqual(result["directory"], spec["name"] + "-seed-1")
        self.assertFalse(result["passed"])


class NativeTraceContract(unittest.TestCase):
    def test_native_lane_records_the_saved_board_and_the_verdict(self):
        from trace_native import NativeTrace

        from pnr import trace
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

        spec = designs()[0]
        pads = [
            Pad("1", "GND", (-0.9375, 0.0), (0.975, 1.4)),
            Pad("2", "LED_A", (0.9375, 0.0), (0.975, 1.4)),
        ]
        graph = BoardGraph(
            "led",
            [Component("D1", "LED", (5.0, 5.0), 0.0, "top", (3.0, 1.8), (3.0, 1.8), pads=pads)],
            [Net("LED_A", 1, [("D1", "1"), ("D1", "2")])],
            BoardOutline(18.0, 14.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "trace").mkdir(parents=True)
            (root / "source.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pcb").write_text(BOARD)
            native = NativeTrace(
                root, spec, 0, parser().parse_args(["--out", "x", "--trace"]), "run"
            )
            self.assertEqual(
                native.environment(),
                dict(PNR_TRACE_DIR=str(root / "trace"), PNR_TRACE_LANE="engine"),
            )
            dense = NativeTrace(
                Path(tmp) / "dense",
                spec,
                0,
                parser().parse_args(["--out", "x", "--trace", "--trace-placement-every", "5"]),
                "run",
            )
            self.assertEqual(dense.environment()["PNR_TRACE_PLACEMENT_EVERY"], "5")
            dense_run = json.loads((Path(tmp) / "dense" / "trace" / "run.json").read_text())
            self.assertEqual(dense_run["config"]["trace_placement_every"], 5)
            run = json.loads((root / "trace" / "run.json").read_text())
            self.assertEqual(
                (run["subject"]["case"], run["config"]["initial_pool"]), (spec["name"], False)
            )
            self.assertNotIn("trace_placement_every", run["config"])  # recorded only when set
            recorder = trace.Recorder(root / "trace")
            recorder.begin_board(graph)
            recorder.close()
            drc = dict(
                unconnected_items=[
                    dict(items=[dict(pos=dict(x=11, y=21)), dict(pos=dict(x=12, y=22))])
                ],
                violations=[],
            )
            native.finish(
                root / "routed.kicad_pcb",
                drc,
                dict(
                    case=spec["name"],
                    seed=0,
                    passed=False,
                    reasons=["native_unconnected_items"],
                    opens=1,
                    violations={},
                    vias=0,
                    copper_length_mm=4.0,
                ),
            )
            events = [
                json.loads(line)
                for line in (root / "trace" / "streams" / "native.jsonl").read_text().splitlines()
            ]
            header = json.loads((root / "trace" / "header.json").read_text())
            self.assertFalse((root / "trace" / "errors.json").exists())
        self.assertEqual([e["kind"] for e in events], ["board", "result"])
        board, result = events
        self.assertEqual((board["stage"], board["drc"]["unconnected"]), ("refill", 1))
        self.assertEqual(board["drc"]["open_pairs"], [[1000, 13000, 2000, 12000]])
        self.assertEqual(board["progress"], dict(done=0, total=1, source="kicad"))
        self.assertEqual((result["passed"], result["opens"], result["rules"]), (False, 1, {}))
        pad = header["components"][0]["pads"][0]
        self.assertEqual(
            (pad["shape"], pad["corner"], header["components"][0]["value"]),
            ("roundrect", 244, "red"),
        )

    def test_snapshot_judges_with_the_custom_rules_and_keeps_no_library_paths(self):
        from trace_native import NativeTrace

        from pnr import trace
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

        # A stand-in kicad-cli: the DRC report says which of the board's side files it saw.
        cli_source = (
            "import json,sys\nfrom pathlib import Path\n"
            "board=Path(sys.argv[3]);out=Path(sys.argv[sys.argv.index('--output')+1])\n"
            "seen=[s for s in ('.kicad_pro','.kicad_dru') if board.with_suffix(s).is_file()]\n"
            "seen+=['fp-lib-table'] if (board.parent/'fp-lib-table').is_file() else []\n"
            "out.write_text(json.dumps(dict(unconnected_items=[],violations=[dict(type=s) for s in seen])))\n"
        )
        pads = [
            Pad("1", "GND", (-0.9375, 0.0), (0.975, 1.4)),
            Pad("2", "LED_A", (0.9375, 0.0), (0.975, 1.4)),
        ]
        graph = BoardGraph(
            "led",
            [Component("D1", "LED", (5.0, 5.0), 0.0, "top", (3.0, 1.8), (3.0, 1.8), pads=pads)],
            [Net("LED_A", 1, [("D1", "1"), ("D1", "2")])],
            BoardOutline(18.0, 14.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "trace").mkdir(parents=True)
            cli = Path(tmp) / "kicad-cli"
            cli.write_text("#!" + sys.executable + "\n" + cli_source)
            cli.chmod(0o755)
            (root / "source.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pcb").write_text(BOARD)
            (root / "routed.kicad_pro").write_text("{}")
            (root / "routed.kicad_dru").write_text("(version 1)\n")
            (root / "fp-lib-table").write_text(
                '(fp_lib_table (lib (name "X") (uri "/abs/X.pretty")))\n'
            )
            native = NativeTrace(
                root, designs()[0], 0, parser().parse_args(["--out", "x", "--trace"]), "run"
            )
            recorder = trace.Recorder(root / "trace")
            recorder.begin_board(graph)
            recorder.close()
            native.snapshot("writeback", root / "routed.kicad_pcb", str(cli), 60)
            folder = root / "trace" / "native" / "writeback"
            self.assertFalse((root / "trace" / "errors.json").exists())
            self.assertTrue((folder / "board.kicad_dru").is_file())
            self.assertFalse((folder / "fp-lib-table").exists())
            events = [
                json.loads(line)
                for line in (root / "trace" / "streams" / "native.jsonl").read_text().splitlines()
            ]
        self.assertEqual(
            events[0]["drc"]["by_type"], {".kicad_pro": 1, ".kicad_dru": 1, "fp-lib-table": 1}
        )

    def test_tracing_errors_never_fail_a_case(self):
        from trace_native import NativeTrace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            native = NativeTrace(
                root, designs()[0], 0, parser().parse_args(["--out", "x", "--trace"]), "run"
            )
            native.snapshot("writeback", root / "missing.kicad_pcb", "kicad-cli", 5)
            self.assertTrue((root / "trace" / "errors.json").is_file())
            native.finish(root / "missing.kicad_pcb", {}, {})


def drc_report(opens=0, items=(), **violations):
    """A kicad-cli DRC report; ``items``: the uuids of every violation's items."""
    return dict(
        unconnected_items=[dict(items=[])] * opens,
        violations=[
            dict(type=t, items=[dict(uuid=u) for u in items])
            for t, n in violations.items()
            for _ in range(n)
        ],
    )


# A two-segment board; %s names the second segment's layer (input or glossed copper).
GLOSS_BOARD = (
    "(kicad_pcb\n\t(segment\n\t\t(start 1 1)\n\t\t(end 2 1)\n\t\t(width 0.25)\n"
    '\t\t(layer "F.Cu")\n\t\t(net "N")\n\t\t(uuid "a")\n\t)\n\t(segment\n'
    '\t\t(start 2 1)\n\t\t(end 3 2)\n\t\t(width 0.25)\n\t\t(layer "%s")\n'
    '\t\t(net "N")\n\t\t(uuid "b")\n\t)\n)\n'
)


class GlossStageContract(unittest.TestCase):
    """run.py --gloss (PNR_GLOSS, opt-in): one gated stage after refill, stubbed here."""

    def test_options_are_opt_in(self):
        args = parser().parse_args(["--out", "x"])
        self.assertEqual((args.gloss, args.gloss_flag, args.gloss_measure), (False, [], False))
        args = parser().parse_args(
            ["--out", "x", "--gloss", "--gloss-flag", "PNR_GLOSS_STEPS=dekink", "--gloss-measure"]
        )
        self.assertEqual((args.gloss, args.gloss_flag), (True, ["PNR_GLOSS_STEPS=dekink"]))

    def test_flags_are_gloss_sub_flags_only(self):
        repo = Path("/repo")
        self.assertEqual(
            gloss_flags(
                ["PNR_GLOSS_STEPS=dekink,gloss", "PNR_GLOSS_CLASSES=docs/examples/g.json"], repo
            ),
            dict(
                PNR_GLOSS_STEPS="dekink,gloss",
                PNR_GLOSS_CLASSES=str((repo / "docs/examples/g.json").resolve()),
            ),
        )
        for bad in ("PNR_SHOVE=1", "PNR_GLOSS_STEPS", "STEPS=dekink", "PNR_GLOSS=1"):
            with self.assertRaises(ValueError, msg=bad):
                gloss_flags([bad], repo)

    def test_outer_gate(self):
        self.assertEqual(gloss_gate(drc_report(1, clearance=1), drc_report(1, clearance=1)), [])
        self.assertEqual(gloss_gate(drc_report(1, clearance=2), drc_report(0, clearance=1)), [])
        self.assertEqual(gloss_gate(drc_report(0), drc_report(1)), ["opens 0 -> 1"])
        self.assertEqual(
            gloss_gate(drc_report(0, clearance=2), drc_report(0, clearance=1, track_dangling=1)),
            ["track_dangling 0 -> 1"],
        )
        # as strict as the pass's own gate (pnr.via_coalesce.violation_keys): a violation that
        # moved to other items is new, even when its type's count holds
        self.assertEqual(
            gloss_gate(
                drc_report(0, ["a", "b"], clearance=1), drc_report(0, ["a", "c"], clearance=1)
            ),
            ["new clearance 1"],
        )
        self.assertEqual(
            gloss_gate(
                drc_report(0, ["b", "a"], clearance=1), drc_report(0, ["a", "b"], clearance=1)
            ),
            [],
        )
        from run import violation_keys

        from pnr.via_coalesce import violation_keys as engine_keys

        for report in (drc_report(0, ["x", "a"], clearance=2, track_dangling=1), drc_report(3)):
            self.assertEqual(violation_keys(report), engine_keys(report))

    def stage(self, after_drc=None, accepted=1, fail=None):
        """Run gloss_stage with a stub ``run``: returns (block, board bytes, stage names)."""
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "case"
        root.mkdir()
        board = root / "routed.kicad_pcb"
        board.write_text(GLOSS_BOARD % "input")
        args = SimpleNamespace(
            python="py", kicad_cli="kicad-cli", kicad_python="kp", gloss=True, timeout=600
        )
        names = []

        def run(name, cmd, extra=None):
            names.append(name)
            cmd = [str(c) for c in cmd]
            if name == fail:
                raise subprocess.CalledProcessError(1, cmd)
            if name.startswith("gloss-drc"):
                report = after_drc if name == "gloss-drc-after" else drc_report(0, clearance=1)
                Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(report))
            elif name == "gloss":
                self.assertEqual(extra["PNR_GLOSS"], "1")
                self.assertEqual(extra["PNR_GLOSS_STEPS"], "dekink")
                self.assertEqual(extra["PNR_GLOSS_CLASSES"], str(tmp / "groups.json"))
                self.assertNotIn("--guard-open-nets", cmd)  # 07g semantics
                Path(cmd[cmd.index("--out") + 1]).write_text(GLOSS_BOARD % "glossed")
                Path(cmd[cmd.index("--report") + 1]).write_text(
                    json.dumps(
                        dict(
                            status="ok",
                            accepted_transactions=accepted,
                            edits_by_step=dict(dekink=3),
                            transactions=[dict(folder=str(tmp))],
                            cross_group=dict(groups=str(tmp / "groups.json"), cap_mm=10.0),
                        )
                    )
                )

        (tmp / "groups.json").write_text('{"classes": {"g": ["N"]}}')
        flags = dict(PNR_GLOSS_STEPS="dekink", PNR_GLOSS_CLASSES=str(tmp / "groups.json"))
        block = gloss_stage(root, board, args, run, flags)
        self.assertEqual((root / "routed.pre-gloss.kicad_pcb").read_text(), GLOSS_BOARD % "input")
        self.assertNotIn(str(tmp), json.dumps(block))  # result.json holds no paths
        return block, board.read_text(), names

    def test_a_clean_pass_replaces_the_board(self):
        block, text, names = self.stage(after_drc=drc_report(0, clearance=1))
        self.assertEqual(text, GLOSS_BOARD % "glossed")
        self.assertTrue(block["kept"] and block["outer_gate"]["passed"])
        self.assertEqual(names, ["gloss-drc-before", "gloss", "gloss-drc-after"])
        self.assertEqual(block["summary"]["edits_by_step"], dict(dekink=3))
        self.assertEqual(block["summary"]["cross_group"]["groups"], "groups.json")
        self.assertEqual(
            block["flags"], dict(PNR_GLOSS_STEPS="dekink", PNR_GLOSS_CLASSES="groups.json")
        )
        self.assertEqual(
            block["groups"],
            dict(
                name="groups.json",
                sha256=hashlib.sha256(b'{"classes": {"g": ["N"]}}').hexdigest(),
            ),
        )
        self.assertNotIn("transactions", block["summary"])
        self.assertEqual(
            block["pre_gloss_board_sha256"],
            hashlib.sha256((GLOSS_BOARD % "input").encode()).hexdigest(),
        )
        self.assertNotEqual(block["pre_gloss_copper_sha256"], block["copper_sha256"])

    def test_a_worse_drc_restores_the_pre_gloss_board(self):
        for after in (drc_report(1, clearance=1), drc_report(0, clearance=2)):
            block, text, _ = self.stage(after_drc=after)
            self.assertEqual(text, GLOSS_BOARD % "input")
            self.assertFalse(block["kept"] or block["outer_gate"]["passed"])
            self.assertEqual(block["board_sha256"], block["pre_gloss_board_sha256"])
            self.assertEqual(block["copper_sha256"], block["pre_gloss_copper_sha256"])

    def test_no_accepted_transaction_keeps_the_board_without_a_second_drc(self):
        block, text, names = self.stage(accepted=0)
        self.assertEqual((text, block["kept"]), (GLOSS_BOARD % "input", False))
        self.assertEqual(names, ["gloss-drc-before", "gloss"])

    def test_a_stage_error_restores_the_board_and_is_reported(self):
        for name in ("gloss", "gloss-drc-after"):
            block, text, _ = self.stage(after_drc=drc_report(0), fail=name)
            self.assertEqual(
                (text, block["kept"], block["status"]), (GLOSS_BOARD % "input", False, "error")
            )

    def test_copper_hash_ignores_uuids_and_order(self):
        tmp = Path(tempfile.mkdtemp())
        a, b, c = tmp / "a.kicad_pcb", tmp / "b.kicad_pcb", tmp / "c.kicad_pcb"
        a.write_text(GLOSS_BOARD % "F.Cu")
        b.write_text((GLOSS_BOARD % "F.Cu").replace('"a"', '"z"').replace('"b"', '"y"'))
        c.write_text(GLOSS_BOARD % "B.Cu")
        self.assertEqual(copper_sha(a), copper_sha(b))
        self.assertNotEqual(copper_sha(a), copper_sha(c))
        # a segment drawn the other way is the same copper
        d = tmp / "d.kicad_pcb"
        d.write_text(
            (GLOSS_BOARD % "F.Cu")
            .replace("(start 2 1)", "(start 3 2)", 1)
            .replace("(end 3 2)", "(end 2 1)", 1)
        )
        self.assertEqual(copper_sha(a), copper_sha(d))
        self.assertNotEqual((GLOSS_BOARD % "F.Cu"), d.read_text())
        # a layout it cannot read (here: spaces, not tabs) raises; a board without copper hashes
        e = tmp / "e.kicad_pcb"
        e.write_text((GLOSS_BOARD % "F.Cu").replace("\t", "  "))
        with self.assertRaises(ValueError):
            copper_sha(e)
        f = tmp / "f.kicad_pcb"
        f.write_text("(kicad_pcb\n\t(pcbplotparams\n\t\t(viasonmask no)\n\t)\n)\n")
        self.assertEqual(copper_sha(f), hashlib.sha256(b"").hexdigest())

    def test_summary_and_measure_figures(self):
        self.assertEqual(
            gloss_summary(dict(status="ok", inverse_specs=[1], steps_report={}, wall_seconds=3)),
            dict(status="ok", wall_seconds=3),
        )
        # a pass cut short by its wall-clock budget is visible in result.json
        planning = dict(inventories=4, deadline_hits=1, rows=[dict(seconds=120.0)])
        self.assertEqual(
            gloss_summary(dict(stop="time_budget", budget=dict(seconds=240.0), planning=planning)),
            dict(
                stop="time_budget",
                budget=dict(seconds=240.0),
                planning=dict(inventories=4, deadline_hits=1),
            ),
        )
        row = dict(
            objective=[0, 0, 0, 0, 0, 0],
            drc=dict(violations=0, unconnected=0, types={}),
            audit=dict(subwidth=0),
            pad_entry=dict(blocked=0),
            seconds=1.5,
            metrics=dict(
                classes=dict(eligible=dict(length_mm=12.34567, segments=9, bends_all=7)),
                X_mm2=1.23456,
                T_mm=2.0,
                DS_mm2=0.5,
                A3_mm2=100.0,
                cross_group=dict(max_mm=4.0, max_pair=["A", "B"], over_cap=[]),
            ),
        )
        summary = measure_summary(row)
        self.assertEqual(
            (summary["length_mm"], summary["bends_all"], summary["X_mm2"]), (12.346, 7, 1.235)
        )
        self.assertEqual((summary["cross_group_max_mm"], summary["cross_group_over_cap"]), (4.0, 0))

    def test_a_groups_file_is_frozen_and_recorded_by_name_and_content(self):
        tmp = Path(tempfile.mkdtemp())
        groups = tmp / "live" / "groups.json"
        groups.parent.mkdir()
        groups.write_text('{"classes": {"g": ["N"]}}')
        flags, record = freeze_gloss_groups(
            dict(PNR_GLOSS_CLASSES=str(groups), PNR_GLOSS_STEPS="dekink"), tmp / "freeze"
        )
        frozen = tmp / "freeze" / "gloss-groups" / "groups.json"
        self.assertEqual(flags, dict(PNR_GLOSS_CLASSES=str(frozen), PNR_GLOSS_STEPS="dekink"))
        self.assertEqual(frozen.read_text(), groups.read_text())
        self.assertEqual(
            record, dict(name="groups.json", sha256=hashlib.sha256(frozen.read_bytes()).hexdigest())
        )
        groups.write_text("{}")  # a later edit of the live file does not reach the run
        self.assertNotEqual(frozen.read_text(), groups.read_text())
        self.assertEqual(
            freeze_gloss_groups(dict(PNR_GLOSS_STEPS="x"), tmp / "f2"),
            (dict(PNR_GLOSS_STEPS="x"), None),
        )
        with self.assertRaises(ValueError):
            freeze_gloss_groups(dict(PNR_GLOSS_CLASSES=str(tmp / "missing.json")), tmp / "f3")

    def test_the_public_example_groups_cover_only_nets_of_their_design(self):
        from pnr.gloss import load_class_tags

        example = Path(__file__).resolve().parents[3] / "docs/examples/gloss-groups-chaser.json"
        tags = load_class_tags(str(example))
        spec = next(c for c in designs() if c["name"] == "07-chaser-20")
        nets = {n for p in spec["parts"] for n in p["pins"].values() if n}
        self.assertEqual(set(tags) - nets, set())
        self.assertEqual(sorted(set(tags.values())), ["clock_reset", "led_drive", "timer"])
        self.assertEqual(nets - set(tags), {"GND", "VCC"})  # the rails: their own net classes


class LadderResultsContract(unittest.TestCase):
    def test_case_result_names_the_rules_and_holds_no_paths(self):
        from animate_ladder import case_result

        spec = designs()[4]
        drc = dict(
            unconnected_items=[],
            violations=[
                dict(
                    type="clearance",
                    description=(
                        "Clearance violation (rule 'jlc-pofv_via_to_smd_pad' clearance 0.1270 mm; "
                        "actual 0.0000 mm)"
                    ),
                ),
                dict(
                    type="clearance",
                    description=(
                        "Clearance violation (rule 'jlc-pofv_via_to_smd_pad' clearance 0.1270 mm; "
                        "actual 0.0034 mm)"
                    ),
                ),
                dict(type="silk_overlap", description="Silkscreen overlap"),
            ],
        )
        pnr = dict(converged=True, unrouted=[], deferred=[], initial_pool=dict(selected="start-05"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / (spec["name"] + "-seed-0")
            root.mkdir()
            (root / "design.json").write_text(json.dumps(spec))
            (root / "drc.json").write_text(json.dumps(drc))
            (root / "routed.kicad_dru").write_text(
                "(version 1)\n# Generated by pnr.fab_profile (profile jlc-pofv); notes\n"
            )
            result = dict(
                new_result(spec, 0, root),
                reasons=["native_drc_violations"],
                opens=0,
                violations={"clearance": 2, "silk_overlap": 1},
                vias=7,
                tracks=300,
                copper_length_mm=120.1234,
                pnr=pnr,
                elapsed_seconds=32.44,
            )
            entry = case_result(spec["name"], root, result, ["--initial-pool"])
        self.assertEqual((entry["parts"], entry["nets"], entry["layers"]), (10, 7, 2))
        self.assertEqual(entry["drc_rules"], {"jlc-pofv_via_to_smd_pad": 2, "silk_overlap": 1})
        self.assertEqual(
            (entry["fab_profile"], entry["routed"], entry["passed"]), ("jlc-pofv", True, False)
        )
        self.assertEqual(
            (entry["copper_length_mm"], entry["seconds"], entry["selected_start"]),
            (120.12, 32.4, "start-05"),
        )
        self.assertNotIn(tmp, json.dumps(entry))

    def test_manifest_provenance_comes_from_the_ladder_run(self):
        from animate_ladder import README_CASES, case_result, ladder_provenance

        self.assertEqual(README_CASES[0], "05-timer-led-10")  # the 555 flasher
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            doc = dict(
                engine_revision="abc",
                engine_dirty=False,
                sources_sha256="f" * 64,
                fab_profile="legacy",
                platform="linux-aarch64",
                arguments=dict(python=tmp + "/venv/bin/python"),
            )
            (run / "provenance.json").write_text(json.dumps(doc))
            info = ladder_provenance(
                run, image="ghcr.io/example/yapnr@sha256:" + "0" * 64, kicad="10.0.6"
            )
            spec = designs()[0]
            root = run / (spec["name"] + "-seed-0")
            root.mkdir()
            (root / "design.json").write_text(json.dumps(spec))
            entry = case_result(
                spec["name"], root, dict(new_result(spec, 0, root), passed=True), [], "legacy"
            )
        self.assertEqual(
            (info["engine_revision"], info["engine_dirty"], info["fab_profile"]),
            ("abc", False, "legacy"),
        )
        self.assertEqual((info["platform"], info["kicad"]), ("linux-aarch64", "10.0.6"))
        self.assertNotIn(tmp, json.dumps(info))
        self.assertEqual(entry["fab_profile"], "legacy")  # no .kicad_dru: the run's profile


class SuiteEnvScrubbing(unittest.TestCase):
    """An operator's ambient PNR_* switches must not silently change the suite, but
    PNR_LIVE_* (telemetry the yapnr exp wrapper sets so a task reports to the live
    viewer, hardware/pnr/pnr/live.py) is not a suite switch and must survive (review
    finding: ladder-cell tasks previously emitted no live events because this filter
    deleted PNR_LIVE_DIR/PNR_LIVE_CANDIDATE before run.py ever launched a case)."""

    def test_live_vars_pass_ambient_switches_are_stripped(self):
        base = {
            "PATH": "/usr/bin",
            "PNR_LIVE_DIR": "/scratch/live",
            "PNR_LIVE_CANDIDATE": "task-7",
            "PNR_LIVE_ITERATION": "3",
            "PNR_LOCAL_PRESSURE": "0",  # ambient copy; overridden below like run.py does
            "PNR_JOINT_ACCESS": "1",
            "PNR_PACKED_MAZE": "1",  # an ambient switch that must NOT leak into the suite
        }
        env = scrubbed_suite_env(base, PNR_LOCAL_PRESSURE="1")
        self.assertEqual(env["PNR_LIVE_DIR"], "/scratch/live")
        self.assertEqual(env["PNR_LIVE_CANDIDATE"], "task-7")
        self.assertEqual(env["PNR_LIVE_ITERATION"], "3")
        self.assertEqual(env["PNR_LOCAL_PRESSURE"], "1")  # override wins
        self.assertEqual(env["PNR_JOINT_ACCESS"], "1")
        self.assertNotIn("PNR_PACKED_MAZE", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_the_profile_dir_passes(self):
        # Telemetry like PNR_LIVE_*: yapnr exp's [profile] sets it for pnr.profile records.
        env = scrubbed_suite_env({"PNR_PROFILE_DIR": "/work/profiles", "PNR_SHRINK": "1"})
        self.assertEqual(env["PNR_PROFILE_DIR"], "/work/profiles")
        self.assertNotIn("PNR_SHRINK", env)

    def test_no_live_vars_is_unaffected(self):
        env = scrubbed_suite_env({"PATH": "/usr/bin", "PNR_SHRINK": "1"}, PNR_LOCAL_PRESSURE="1")
        self.assertNotIn("PNR_SHRINK", env)
        self.assertEqual(env["PNR_LOCAL_PRESSURE"], "1")


class CaseLaneLiveEmit(unittest.TestCase):
    """run.py's own terminal event for a case/seed lane (hardware/pnr/pnr/live.py's ``emit``,
    reused directly rather than reimplemented): a no-op with PNR_LIVE_DIR unset -- the common,
    non-live run, so this must never change run.py's own results -- and, with it set, a real
    event under the task's own PNR_LIVE_CANDIDATE (yapnr.exp.task's task id for a live campaign,
    e.g. a ladder "case" lane: docs/viewer.md "Finished without a final event")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for var in ("PNR_LIVE_DIR", "PNR_LIVE_CANDIDATE", "PNR_LIVE_ITERATION"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def test_is_a_no_op_with_live_off(self):
        live_emit("case_complete", data=dict(passed=True))
        self.assertEqual(list(self.tmp.iterdir()), [])  # nothing written anywhere

    def test_writes_a_real_event_under_the_tasks_own_candidate_id(self):
        os.environ["PNR_LIVE_DIR"] = str(self.tmp)
        os.environ["PNR_LIVE_CANDIDATE"] = "ladder/09-mcu-usb/s0"
        live_emit("case_complete", data=dict(passed=True, opens=0, violations=0, case="09-mcu-usb"))
        files = list((self.tmp / "events").glob("*.json"))
        self.assertEqual(len(files), 1)
        event = json.loads(files[0].read_text())
        self.assertEqual(event["kind"], "case_complete")
        self.assertEqual(event["candidate"], "ladder/09-mcu-usb/s0")
        self.assertEqual(event["data"]["passed"], True)

    def test_a_failed_case_emits_case_failed(self):
        os.environ["PNR_LIVE_DIR"] = str(self.tmp)
        os.environ["PNR_LIVE_CANDIDATE"] = "ladder/09-mcu-usb/s0"
        live_emit("case_failed", data=dict(passed=False, reasons=["designed_open"]))
        files = list((self.tmp / "events").glob("*.json"))
        event = json.loads(files[0].read_text())
        self.assertEqual(event["kind"], "case_failed")
        self.assertEqual(event["data"]["reasons"], ["designed_open"])


if __name__ == "__main__":
    unittest.main()
