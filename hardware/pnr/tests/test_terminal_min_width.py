"""@pnr-terminal-width: source min/preferred terminal width contracts (N-0002 part 1).

Parsing and resolution always run; rules.json gains ``terminal_width_intents``
and geometry changes only with PNR_TERMINAL_MIN_WIDTH=1. The required width is a
hard pad-entry floor; the preferred width is tried first by the plane fanout and
falls back to the required width where clearance forbids it.

Pure tests run under any python. Geometry tests need pcbnew (KiCad python).
Opt-in: the Mini source annotations resolve against inputs10b/graph.json when
that file exists (skipped otherwise).
"""

import atexit
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from pnr.electrical import (
    annotations,
    compile_policy,
    resolve_currents,
    terminal_policy,
    terminal_width,
    terminal_width_annotation,
)
from pnr.pad_entry import entry_widths, required_width, width_choice

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "splanc_dev/elec/src/splanc_mini.ato"
BASE_SRC = HERE.parents[3] / "src14.base/hardware/splanc_dev/elec/src/splanc_mini.ato"
GRAPH = HERE.parents[3] / "inputs10b/graph.json"
FAB = dict(
    outer_copper_oz=1,
    inner_copper_oz=1,
    delta_t_c=40,
    via_drill_mm=0.3,
    via_diameter_mm=0.6,
    min_via_plating_um=20,
    board_thickness_mm=1.6,
    copper_resistivity_ohm_mm=2.1e-5,
    via_barrel_loss_budget_w=0.01,
    via_array_peak_drop_v=0.01,
)
LINE = (
    '# @pnr-terminal-width {"target":"pd.input_dec_0","pads":["2"],"scope":"terminal",'
    '"net":"lv","min_width_mm":0.254,"preferred_width_mm":0.406}'
)
CURRENT = '# @pnr-current {"target":"pd.ctrl","pads":["20"],"scope":"net","rms_current_a":5,"peak_current_a":5}'
ON = {"PNR_TERMINAL_MIN_WIDTH": "1"}
OFF = {"PNR_TERMINAL_MIN_WIDTH": ""}


def components():
    return [
        NS(
            ref="C53",
            address="board.pd.input_dec_0",
            pads=[NS(name="1", net="raw_usb-hv"), NS(name="2", net="lv")],
        ),
        NS(
            ref="C61",
            address="board.pd.output_dec_0",
            pads=[NS(name="1", net="negotiated-hv"), NS(name="2", net="lv")],
        ),
        NS(
            ref="U7",
            address="board.pd.ctrl",
            pads=[NS(name="20", net="raw_usb-hv"), NS(name="21", net="raw_usb-hv")],
        ),
    ]


_SCRATCH = tempfile.TemporaryDirectory(prefix="pnr-width-test-")
atexit.register(_SCRATCH.cleanup)  # the .ato files written by these tests go away at exit


def write(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".ato", delete=False, dir=_SCRATCH.name)
    handle.write(text)
    handle.close()
    return handle.name


def base_rules():
    return dict(
        fab={"track_width_mm": 0.2, "clearance_mm": 0.15},
        net_classes=[dict(name="gnd", nets=["lv"], plane_layer="In1.Cu", width_mm=None)],
    )


class FakePad:
    def __init__(self, ref, number, net):
        self.ref, self.number, self.net = ref, number, net

    def GetParentFootprint(self):
        return NS(GetReference=lambda: self.ref)

    def GetNumber(self):
        return self.number

    def GetNetname(self):
        return self.net


class ParseTest(unittest.TestCase):
    def test_valid_body_and_defaults(self):
        a = terminal_width_annotation(LINE.split("# @pnr-terminal-width ", 1)[1])
        self.assertEqual(
            (a["contract"], a["min_width_mm"], a["preferred_width_mm"], a["net"]),
            ("terminal_width", 0.254, 0.406, "lv"),
        )
        b = terminal_width_annotation(
            '{"target":"x","pads":["1"],"scope":"terminal","min_width_mm":0.3}'
        )
        self.assertEqual(b["preferred_width_mm"], 0.3)

    def test_rejections(self):
        bad = [
            '{"target":"x","pads":["1"],"scope":"net","min_width_mm":0.3}',  # scope
            '{"target":"x","pads":["1"],"min_width_mm":0.3}',  # scope missing
            '{"target":"x","pads":[],"scope":"terminal","min_width_mm":0.3}',  # no pads
            '{"target":"x","pads":["1","1"],"scope":"terminal","min_width_mm":0.3}',  # duplicate pad
            '{"target":"","pads":["1"],"scope":"terminal","min_width_mm":0.3}',  # no target
            '{"target":"x","pads":["1"],"scope":"terminal","min_width_mm":0}',  # non-positive
            '{"target":"x","pads":["1"],"scope":"terminal"}',  # no minimum
            '{"target":"x","pads":["1"],"scope":"terminal","min_width_mm":0.4,"preferred_width_mm":0.3}',
            '{"target":"x","pads":["1"],"scope":"terminal","min_width":0.3}',  # typo key
            '{"target":"x","pads":["1"],"scope":"terminal","net":"","min_width_mm":0.3}',
            "[1]",
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                terminal_width_annotation(body)

    def test_annotations_mixes_both_tags_with_provenance(self):
        path = write("\n".join(["module X:", "    " + CURRENT, "    " + LINE, ""]))
        records = annotations([path])
        self.assertEqual([r.get("contract") for r in records], [None, "terminal_width"])
        self.assertEqual(records[1]["source"]["line"], 3)
        self.assertEqual(
            records[1]["source"]["sha256"], hashlib.sha256(Path(path).read_bytes()).hexdigest()
        )


class ResolveTest(unittest.TestCase):
    def records(self, *extra):
        return annotations([write("\n".join(["    " + CURRENT, "    " + LINE, *extra, ""]))])

    def test_exact_pad_on_ground_net(self):
        r = resolve_currents(self.records(), components())
        self.assertEqual(
            [(x["ref"], x["net"], x.get("contract")) for x in r],
            [("U7", "raw_usb-hv", None), ("C53", "lv", "terminal_width")],
        )

    def test_net_mismatch_missing_target_missing_pad_and_overlap(self):
        cases = [
            LINE.replace('"net":"lv"', '"net":"gnd"'),
            LINE.replace("pd.input_dec_0", "pd.input_dec_9"),
            LINE.replace('"pads":["2"]', '"pads":["3"]'),
            LINE.replace('"pads":["2"]', '"pads":["1","2"]'),  # spans two nets
            LINE,
        ]  # second contract on C53.2
        for line in cases:
            with self.subTest(line=line), self.assertRaises(ValueError):
                resolve_currents(self.records("    " + line), components())

    def test_subboard_skips_other_blocks(self):
        with mock.patch.dict(os.environ, {"PNR_SUBBOARD": "1"}):
            r = resolve_currents(
                self.records("    " + LINE.replace("pd.input_dec_0", "pd.elsewhere")), components()
            )
        self.assertEqual(len(r), 2)

    def test_current_error_text_unchanged(self):
        with self.assertRaisesRegex(ValueError, "^ambiguous/missing current target nope$"):
            resolve_currents([dict(target="nope", pads=["1"])], components())


class CompileTest(unittest.TestCase):
    def compiled(self, env):
        resolved = resolve_currents(
            annotations([write("    " + CURRENT + "\n    " + LINE + "\n")]), components()
        )
        with mock.patch.dict(os.environ, env):
            return compile_policy(base_rules(), resolved, FAB), resolved

    def test_flag_off_rules_identical_to_current_only(self):
        rules, resolved = self.compiled(OFF)
        only = compile_policy(base_rules(), [r for r in resolved if r.get("contract") is None], FAB)
        self.assertEqual(json.dumps(rules, sort_keys=True), json.dumps(only, sort_keys=True))
        self.assertNotIn("terminal_width_intents", rules)
        self.assertTrue(all(a.get("contract") is None for a in rules["current_intents"]))

    def test_flag_on_emits_intents(self):
        rules, _ = self.compiled(ON)
        self.assertEqual(
            [(a["ref"], a["pads"], a["net"]) for a in rules["terminal_width_intents"]],
            [("C53", ["2"], "lv")],
        )
        self.assertEqual(len(rules["current_intents"]), 1)


class PolicyTest(unittest.TestCase):
    def rules(self):
        r = dict(
            base_rules(),
            electrical_fab=FAB,
            current_intents=[],
            terminal_width_intents=[
                dict(
                    ref="C53",
                    pads=["2"],
                    net="lv",
                    min_width_mm=0.254,
                    preferred_width_mm=0.406,
                    source={"line": 1},
                )
            ],
        )
        return r

    def test_lookup_needs_flag_ref_net_and_pad(self):
        r = self.rules()
        with mock.patch.dict(os.environ, OFF):
            self.assertIsNone(terminal_width("C53", ["2"], "lv", r))
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(terminal_width("C53", "2", "lv", r)["min_width_mm"], 0.254)
            self.assertIsNone(terminal_width("C53", ["1"], "lv", r))
            self.assertIsNone(terminal_width("C53", ["2"], "raw_usb-hv", r))
            self.assertIsNone(terminal_width("C54", ["2"], "lv", r))

    def test_required_width_is_hard_floor_and_preferred_is_first(self):
        r = self.rules()
        pad, other = FakePad("C53", "2", "lv"), FakePad("C53", "1", "raw_usb-hv")
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(required_width(pad, r), 0.2)
            self.assertEqual(entry_widths(pad, r), [0.2])
            self.assertIsNone(width_choice(pad, r, 0.2))
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(required_width(pad, r), 0.254)
            self.assertEqual(entry_widths(pad, r), [0.406, 0.254])
            self.assertEqual(entry_widths(other, r), [0.2])
            self.assertEqual(width_choice(pad, r, 0.406), "preferred")
            self.assertEqual(width_choice(pad, r, 0.254), "required")
            # A wider current/class requirement is never reduced by the contract;
            # a preferred width at or below it collapses to one candidate.
            r["net_classes"].append(dict(name="wide", nets=["lv"], width_mm=0.5))
            self.assertEqual(required_width(pad, r), 0.5)
            self.assertEqual(entry_widths(pad, r), [0.5])

    def test_terminal_current_policy_gets_the_floor(self):
        r = self.rules()
        r["current_intents"] = [
            dict(
                ref="C53",
                pads=["2"],
                net="lv",
                scope="terminal",
                rms_current_a=0.001,
                peak_current_a=0.001,
                source={},
            )
        ]
        with mock.patch.dict(os.environ, OFF):
            before = terminal_policy("C53", ["2"], "lv", r)
        with mock.patch.dict(os.environ, ON):
            after = terminal_policy("C53", ["2"], "lv", r)
        self.assertEqual(before["outer_width_mm"], 0.2)
        self.assertNotIn("min_width_mm", before)
        self.assertEqual(
            (after["outer_width_mm"], after["inner_width_mm"], after["preferred_width_mm"]),
            (0.254, 0.254, 0.406),
        )
        self.assertEqual(after["rms_current_a"], before["rms_current_a"])
        with mock.patch.dict(os.environ, ON):  # no current contract: still no current policy
            self.assertIsNone(terminal_policy("C53", ["2"], "lv", self.rules()))


@unittest.skipUnless(GRAPH.exists(), "opt-in: needs output/hier/inputs10b/graph.json")
class MiniSourceTest(unittest.TestCase):
    """The committed Mini annotations resolve to exactly C53-C57/C61-C64 pad 2 on lv."""

    def graph_components(self):
        g = json.loads(GRAPH.read_text())
        return [
            NS(
                ref=c["ref"],
                address=c.get("address", ""),
                pads=[NS(name=p["name"], net=p["net"]) for p in c["pads"]],
            )
            for c in g["components"]
        ]

    def test_resolution(self):
        resolved = resolve_currents(annotations([SRC]), self.graph_components())
        widths = [a for a in resolved if a.get("contract") == "terminal_width"]
        self.assertEqual(
            sorted(a["ref"] for a in widths),
            ["C53", "C54", "C55", "C56", "C57", "C61", "C62", "C63", "C64"],
        )
        self.assertEqual(
            {
                (tuple(a["pads"]), a["net"], a["min_width_mm"], a["preferred_width_mm"])
                for a in widths
            },
            {(("2",), "lv", 0.254, 0.406)},
        )

    @unittest.skipUnless(BASE_SRC.exists(), "needs src14.base")
    def test_flag_off_policy_matches_base_source_except_file_hash(self):
        cs = self.graph_components()
        new_hash = hashlib.sha256(SRC.read_bytes()).hexdigest()
        base_hash = hashlib.sha256(BASE_SRC.read_bytes()).hexdigest()
        with mock.patch.dict(os.environ, OFF):
            new = compile_policy(base_rules(), resolve_currents(annotations([SRC]), cs), FAB)
            old = compile_policy(base_rules(), resolve_currents(annotations([BASE_SRC]), cs), FAB)
        text = json.dumps(new, sort_keys=True).replace(str(SRC), "SRC").replace(new_hash, "H")
        self.assertEqual(
            text,
            json.dumps(old, sort_keys=True).replace(str(BASE_SRC), "SRC").replace(base_hash, "H"),
        )


@unittest.skipUnless(importlib.util.find_spec("pcbnew") is not None, "requires KiCad pcbnew")
class GeometryTest(unittest.TestCase):
    """Plane fanout and post-route classification on a small native board."""

    def board(self, pads=((10, 10),)):
        import pcbnew

        b = pcbnew.BOARD()
        edge = pcbnew.PCB_SHAPE(b)
        edge.SetShape(pcbnew.SHAPE_T_RECT)
        edge.SetStart(pcbnew.VECTOR2I(0, 0))
        edge.SetEnd(pcbnew.VECTOR2I(20000000, 20000000))
        edge.SetLayer(pcbnew.Edge_Cuts)
        b.Add(edge)
        net = pcbnew.NETINFO_ITEM(b, "lv")
        b.Add(net)
        fps = []
        for i, (x, y) in enumerate(pads):
            fp = pcbnew.FOOTPRINT(b)
            fp.SetReference("C%d" % (53 + i))
            b.Add(fp)
            fp.SetPosition(pcbnew.VECTOR2I(x * 1000000, y * 1000000))
            pad = pcbnew.PAD(fp)
            pad.SetNumber("2")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            layers = pcbnew.LSET()
            layers.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layers)
            pad.SetSize(pcbnew.VECTOR2I(1000000, 1300000))
            pad.SetPosition(fp.GetPosition())
            pad.SetNet(net)
            fp.Add(pad)
            fps.append(fp)
        return b, net

    def rules(self, refs=("C53",)):
        return dict(
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.15,
                "via_diameter_mm": 0.6,
                "via_drill_mm": 0.3,
            },
            net_classes=[dict(name="gnd", nets=["lv"], plane_layer="In1.Cu")],
            terminal_width_intents=[
                dict(
                    ref=r,
                    pads=["2"],
                    net="lv",
                    min_width_mm=0.254,
                    preferred_width_mm=0.406,
                    source={},
                )
                for r in refs
            ],
        )

    def stubs(self, b):
        import pcbnew

        return sorted(
            (round(t.GetWidth() / 1e3), t.GetEnd().x, t.GetEnd().y)
            for t in b.GetTracks()
            if t.GetClass() == "PCB_TRACK"
        )

    def test_preferred_width_in_open_space(self):
        from pnr.writeback import _dogbone_fanout_net

        b, net = self.board()
        log = []
        with mock.patch.dict(os.environ, ON):
            self.assertEqual(
                _dogbone_fanout_net(b, net.GetNetCode(), rules=self.rules(), width_log=log), 1
            )
        self.assertEqual([w for w, _, _ in self.stubs(b)], [406])
        self.assertEqual(
            [(r["pad"], r["how"], r["choice"]) for r in log], [("C53.2", "dogbone", "preferred")]
        )

    def test_falls_back_to_required_when_clearance_forbids_preferred(self):
        from pnr.native_electrical import Oracle
        from pnr.writeback import _dogbone_fanout_net

        clear = Oracle.clear

        def narrow_only(self, net, layer, a, z, width, *args, **kwargs):
            return width < 0.3 and clear(self, net, layer, a, z, width, *args, **kwargs)

        b, net = self.board()
        log = []
        with mock.patch.dict(os.environ, ON), mock.patch.object(Oracle, "clear", narrow_only):
            self.assertEqual(
                _dogbone_fanout_net(b, net.GetNetCode(), rules=self.rules(), width_log=log), 1
            )
        self.assertEqual([w for w, _, _ in self.stubs(b)], [254])
        self.assertEqual(
            [(r["how"], r["choice"], r["width_mm"]) for r in log], [("dogbone", "required", 0.254)]
        )

    def test_never_below_required(self):
        from pnr.native_electrical import Oracle
        from pnr.writeback import _dogbone_fanout_net

        b, net = self.board()
        log = []
        widths = []

        def record(self, net, layer, a, z, width, *args, **kwargs):
            widths.append(round(width, 3))
            return False

        with mock.patch.dict(os.environ, ON), mock.patch.object(Oracle, "clear", record):
            self.assertEqual(
                _dogbone_fanout_net(b, net.GetNetCode(), rules=self.rules(), width_log=log), 0
            )
        self.assertEqual(set(widths), {0.406, 0.254})
        self.assertEqual([r["how"] for r in log], ["unplaced"])

    def test_flag_off_and_uncontracted_pads_unchanged(self):
        from pnr.writeback import _dogbone_fanout_net

        results = []
        for env, refs in ((OFF, ("C53",)), (ON, ("C99",))):
            b, net = self.board()
            log = []
            with mock.patch.dict(os.environ, env):
                _dogbone_fanout_net(b, net.GetNetCode(), rules=self.rules(refs), width_log=log)
            results.append(self.stubs(b))
            self.assertEqual(log, [])
        self.assertEqual(results[0], results[1])
        self.assertEqual([w for w, _, _ in results[0]], [200])

    def stub_board(self, foreign_y=None):
        """C53.2 with a 0.2 mm macro stub to a via 1.2 mm away (+x), optionally
        a foreign track parallel to it at ``foreign_y`` mm off the stub axis."""
        import pcbnew

        b, net = self.board()
        pad = next(iter(b.FindFootprintByReference("C53").Pads()))
        via = pcbnew.PCB_VIA(b)
        via.SetPosition(pcbnew.VECTOR2I(11200000, 10000000))
        via.SetFrontWidth(600000)
        via.SetDrill(300000)
        via.SetNet(net)
        b.Add(via)
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pad.GetPosition())
        t.SetEnd(via.GetPosition())
        t.SetWidth(200000)
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(net)
        b.Add(t)
        if foreign_y is not None:
            other = pcbnew.NETINFO_ITEM(b, "other")
            b.Add(other)
            f = pcbnew.PCB_TRACK(b)
            f.SetStart(pcbnew.VECTOR2I(10750000, round((10 + foreign_y) * 1e6)))
            f.SetEnd(pcbnew.VECTOR2I(11500000, round((10 + foreign_y) * 1e6)))
            f.SetWidth(200000)
            f.SetLayer(pcbnew.F_Cu)
            f.SetNet(other)
            b.Add(f)
        return b

    def test_repair_widens_existing_macro_stub_preferred_then_required(self):
        from pnr.pad_entry import repair, terminal_width_report

        # 0.42 mm off-axis: 0.406 + clearance collides, 0.254 + clearance clears.
        for foreign_y, width, status in ((None, 0.406, "preferred"), (0.42, 0.254, "required")):
            with self.subTest(foreign_y=foreign_y):
                b = self.stub_board(foreign_y)
                with mock.patch.dict(os.environ, ON):
                    result = repair(b, self.rules())
                    rows = terminal_width_report(b, self.rules())
                self.assertEqual(result["blocked"], [])
                self.assertEqual(
                    [
                        (a["pad"], a["widened"]["width_mm"], a["widened"]["choice"])
                        for a in result["added"]
                    ],
                    [("C53.2", width, status)],
                )
                self.assertEqual([r["status"] for r in rows], [status])
                widths = sorted(
                    t.GetWidth()
                    for t in b.GetTracks()
                    if t.GetClass() == "PCB_TRACK" and t.GetNetname() == "lv"
                )
                self.assertEqual(
                    widths, [200000, round(width * 1e6)]
                )  # old stub kept, never narrowed

    def graze_board(self, foreign_y=None):
        """C53.2 (1.0 x 0.5 mm) grazed on its right edge by a 0.3 mm lv trace (0.1 mm overlap:
        touching, but no full-width entry), optionally a foreign track parallel to the
        centre-branch axis at ``foreign_y`` mm (its near edge ``foreign_y`` - 0.1 mm off it)."""
        import pcbnew

        b, net = self.board()
        pad = next(iter(b.FindFootprintByReference("C53").Pads()))
        pad.SetSize(pcbnew.VECTOR2I(1000000, 500000))
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(10550000, 9000000))
        t.SetEnd(pcbnew.VECTOR2I(10550000, 11000000))
        t.SetWidth(300000)
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(net)
        b.Add(t)
        if foreign_y is not None:
            other = pcbnew.NETINFO_ITEM(b, "other")
            b.Add(other)
            f = pcbnew.PCB_TRACK(b)
            f.SetStart(pcbnew.VECTOR2I(10000000, round((10 + foreign_y) * 1e6)))
            f.SetEnd(pcbnew.VECTOR2I(10300000, round((10 + foreign_y) * 1e6)))
            f.SetWidth(200000)
            f.SetLayer(pcbnew.F_Cu)
            f.SetNet(other)
            b.Add(f)
        return b

    def test_centre_branch_preferred_then_required(self):
        """pad_entry.repair's ordinary centre-to-trace branch also tries the preferred width first."""
        from pnr.pad_entry import repair, terminal_width_report

        # branch axis y = 10; the foreign edge 0.3 mm off it: 0.203 + 0.15 collides, 0.127 + 0.15 clears.
        for foreign_y, width, status in ((None, 0.406, "preferred"), (0.4, 0.254, "required")):
            with self.subTest(foreign_y=foreign_y):
                b = self.graze_board(foreign_y)
                with mock.patch.dict(os.environ, ON):
                    result = repair(b, self.rules())
                    rows = terminal_width_report(b, self.rules())
                self.assertEqual(result["blocked"], [])
                self.assertEqual(
                    [(a["pad"], a["width_mm"], a["choice"]) for a in result["added"]],
                    [("C53.2", width, status)],
                )
                self.assertEqual([r["status"] for r in rows], [status])
        # flag off (no contract): the pre-contract branch at the required width, no 'choice' key
        b = self.graze_board()
        with mock.patch.dict(os.environ, OFF):
            result = repair(b, dict(self.rules(), terminal_width_intents=[]))
        self.assertEqual(
            [(a["pad"], a["width_mm"], "choice" in a) for a in result["added"]],
            [("C53.2", 0.2, False)],
        )

    def test_repair_blocks_when_even_required_width_is_forbidden(self):
        from pnr.pad_entry import repair

        b = self.stub_board(0.33)
        with mock.patch.dict(os.environ, ON):
            result = repair(b, self.rules())
        self.assertEqual([x["pad"] for x in result["blocked"]], ["C53.2"])
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(
                repair(self.stub_board(0.33), self.rules()), dict(added=[], blocked=[])
            )

    def test_post_route_report_and_hard_floor(self):
        import pcbnew

        from pnr.pad_entry import inspect, terminal_width_report

        b, net = self.board(((5, 10), (10, 10), (15, 10)))
        for ref, width in (("C53", 406000), ("C54", 254000), ("C55", 200000)):
            pad = next(iter(b.FindFootprintByReference(ref).Pads()))
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(pad.GetPosition())
            t.SetEnd(pad.GetPosition() + pcbnew.VECTOR2I(0, 2000000))
            t.SetWidth(width)
            t.SetLayer(pcbnew.F_Cu)
            t.SetNet(net)
            b.Add(t)
        rules = self.rules(("C53", "C54", "C55"))
        with mock.patch.dict(os.environ, ON):
            rows = {r["pad"]: r for r in terminal_width_report(b, rules)}
            good = {
                p.GetParentFootprint().GetReference(): ok for p, _, _, _, ok in inspect(b, rules)
            }
        self.assertEqual(
            {k: v["status"] for k, v in rows.items()},
            {"C53.2": "preferred", "C54.2": "required", "C55.2": "below_min"},
        )
        self.assertEqual(rows["C54.2"]["entry_width_mm"], 0.254)
        self.assertEqual(good, {"C53": True, "C54": True, "C55": False})
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(terminal_width_report(b, rules), [])
            self.assertTrue(all(ok for _, _, _, _, ok in inspect(b, rules)))


P027 = HERE.parents[3] / "runs/h6-hier/cand/p027/native"
P027_BOARD_SHA256 = "5f425ec109450fb432569f3c24bf6a6861198e663390248e9f0ebd2e73595935"


@unittest.skipUnless(
    os.environ.get("PNR_E2E_P027") == "1"
    and importlib.util.find_spec("pcbnew") is not None
    and (P027 / "electrical/board.kicad_pcb").exists(),
    "opt-in: PNR_E2E_P027=1, KiCad pcbnew and the H6 p027 routed board",
)
class P027EndToEndTest(unittest.TestCase):
    """H6 best routed board: the reused pd-block ground stubs of C53-C57/C61-C64
    are 0.2 mm. With the flag the final pad-entry repair widens all nine at the
    preferred 0.406 mm (0 DRC violations in the scratch run); flag off: no change."""

    def test_widen_all_nine_preferred(self):
        import pcbnew

        board_path = P027 / "electrical/board.kicad_pcb"
        if hashlib.sha256(board_path.read_bytes()).hexdigest() != P027_BOARD_SHA256:
            self.skipTest("p027 board changed since the fixture was recorded")
        from pnr.pad_entry import repair, terminal_width_report

        rules = json.loads((P027 / "electrical/native-loop/policy/prepare.json").read_text())
        with mock.patch.dict(os.environ, OFF):
            self.assertEqual(
                repair(pcbnew.LoadBoard(str(board_path)), rules), dict(added=[], blocked=[])
            )
        b = pcbnew.LoadBoard(str(board_path))
        cs = [
            NS(
                ref=f.GetReference(),
                address=next(
                    (z.GetText() for z in f.GetFields() if z.GetName() == "atopile_address"), ""
                ),
                pads=[NS(name=p.GetNumber(), net=p.GetNetname()) for p in f.Pads()],
            )
            for f in b.GetFootprints()
        ]
        with mock.patch.dict(os.environ, ON):
            rules["terminal_width_intents"] = [
                a
                for a in resolve_currents(annotations([SRC]), cs)
                if a.get("contract") == "terminal_width"
            ]
            result = repair(b, rules)
            rows = terminal_width_report(b, rules)
        self.assertEqual(result["blocked"], [])
        self.assertEqual(
            sorted(a["pad"] for a in result["added"]),
            ["C53.2", "C54.2", "C55.2", "C56.2", "C57.2", "C61.2", "C62.2", "C63.2", "C64.2"],
        )
        self.assertEqual({r["status"] for r in rows}, {"preferred"})


if __name__ == "__main__":
    unittest.main()
