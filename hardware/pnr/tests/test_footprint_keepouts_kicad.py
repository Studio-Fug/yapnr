"""Footprint rule areas and footprint drills, judged by KiCad itself.

* ingest reads a footprint's own rule areas (pnr.ingest._rule_areas) in the
  footprint's frame, and writeback's KiCad ``Flip``/rotate puts them exactly where
  pnr.fixed_block.footprint_keepouts says, on the mirrored copper layer on the bottom;
* the native Oracle (writeback's plane fallback vias, the repair stages) and the
  shove world read the footprints' rule areas, not only the board's;
* end to end: the engine routes a board whose only path crosses a socket's
  keep-out and passes a USB shield hole, writeback emits it and kicad-cli's DRC
  reports no ``items_not_allowed`` and no ``hole_to_hole``; the same board with the
  areas dropped from the graph (the engine before this change) shows the finding.

The in-process cases need KiCad's Python (``pcbnew``); the end-to-end case needs
``PNR_KICAD_PYTHON`` and ``PNR_KICAD_CLI`` (headless KiCad) and the engine's own
interpreter. Each skips otherwise.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HAVE_PCBNEW = importlib.util.find_spec("pcbnew") is not None
KICAD_PYTHON = os.environ.get("PNR_KICAD_PYTHON", "")
KICAD_CLI = os.environ.get("PNR_KICAD_CLI", "")
PNR_ROOT = Path(__file__).resolve().parents[1]

# The socket's keep-out in the library's frame (y down), off-centre so a wrong
# mirror shows: x -1..1, y -2.6..1.0.
AREA_KICAD = [(-1.0, -2.6), (1.0, -2.6), (1.0, 1.0), (-1.0, 1.0)]


def socket_mod():
    pts = " ".join("(xy %s %s)" % p for p in AREA_KICAD)
    return "\n".join(
        [
            '(footprint "SOCKET"',
            "  (version 20240108)",
            '  (generator "pnr_test")',
            '  (layer "F.Cu")',
            '  (property "Reference" "REF**" (at 0 -4 0) (layer "F.Fab")'
            " (effects (font (size 1 1) (thickness 0.15))))",
            "  (attr smd)",
            "  (fp_rect (start -1.2 -2.8) (end 1.2 2.8) (stroke (width 0.05) (type solid))"
            ' (fill none) (layer "F.CrtYd"))',
            '  (zone (net 0) (net_name "") (layer "F.Cu") (hatch full 0.508)',
            "    (connect_pads (clearance 0)) (min_thickness 0.254)",
            "    (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)"
            " (copperpour not_allowed) (footprints not_allowed))",
            "    (fill (thermal_gap 0.508) (thermal_bridge_width 0.508))",
            "    (polygon (pts %s)))" % pts,
            ")",
            "",
        ]
    )


def tp_mod():
    return "\n".join(
        [
            '(footprint "TP"',
            "  (version 20240108)",
            '  (generator "pnr_test")',
            '  (layer "F.Cu")',
            "  (attr smd)",
            "  (fp_rect (start -0.5 -0.5) (end 0.5 0.5) (stroke (width 0.05) (type solid))"
            ' (fill none) (layer "F.CrtYd"))',
            '  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))',
            ")",
            "",
        ]
    )


def shield_mod():
    return "\n".join(
        [
            '(footprint "SHIELD"',
            "  (version 20240108)",
            '  (generator "pnr_test")',
            '  (layer "F.Cu")',
            "  (attr through_hole)",
            "  (fp_rect (start -0.8 -0.8) (end 0.8 0.8) (stroke (width 0.05) (type solid))"
            ' (fill none) (layer "F.CrtYd"))',
            '  (pad "SH" thru_hole circle (at 0 0) (size 1.45 1.45) (drill 0.85)'
            ' (layers "*.Cu" "*.Mask"))',
            ")",
            "",
        ]
    )


# Builds the source board (KiCad's Python): TP1/TP2 on net A across a 20 x 6 mm
# board, the socket between them (top, rot 0, or as argv[3] says), a shield hole on
# GND below the path. argv: library dir, output board, socket "side rot".
BUILD = r"""
import sys
import pcbnew as k

lib, out = sys.argv[1], sys.argv[2]
side, rot = sys.argv[3].split()
b = k.BOARD()
b.SetCopperLayerCount(2)
nm = lambda v: int(round(v * 1e6))
V = lambda x, y: k.VECTOR2I(nm(x), nm(y))
for a, z in [((0, 0), (20, 0)), ((20, 0), (20, 6)), ((20, 6), (0, 6)), ((0, 6), (0, 0))]:
    s = k.PCB_SHAPE(b)
    s.SetShape(k.SHAPE_T_SEGMENT)
    s.SetStart(V(*a))
    s.SetEnd(V(*z))
    s.SetLayer(k.Edge_Cuts)
    s.SetWidth(nm(0.1))
    b.Add(s)
nets = {}
for name in ("A", "GND"):
    nets[name] = k.NETINFO_ITEM(b, name)
    b.Add(nets[name])

def place(name, ref, at, net=None, flip=False, turn=0):
    fp = k.FootprintLoad(lib, name)
    fp.SetReference(ref)
    b.Add(fp)
    fp.SetPosition(V(*at))
    if flip:
        fp.Flip(fp.GetPosition(), False)
    fp.SetOrientationDegrees(turn)
    for pad in fp.Pads():
        if net:
            pad.SetNet(nets[net])
    return fp

place("TP", "TP1", (3, 3), "A")
place("TP", "TP2", (17, 3), "A")
place("SOCKET", "J3", (10, 3), flip=side == "bottom", turn=float(rot))
place("SHIELD", "J1", (6.5, 4.6), "GND")
k.SaveBoard(out, b)
"""


def write_library(root):
    library = root / "inline.pretty"
    library.mkdir(exist_ok=True)
    (library / "SOCKET.kicad_mod").write_text(socket_mod())
    (library / "TP.kicad_mod").write_text(tp_mod())
    (library / "SHIELD.kicad_mod").write_text(shield_mod())
    return library


@unittest.skipUnless(HAVE_PCBNEW, "requires KiCad's pcbnew")
class IngestWritebackTest(unittest.TestCase):
    def build(self, root, side="top", rot=0):
        import sys

        library = write_library(root)
        board = root / ("source-%s-%s.kicad_pcb" % (side, rot))
        done = subprocess.run(
            [sys.executable, "-c", BUILD, str(library), str(board), "%s %s" % (side, rot)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        return board

    def test_ingest_reads_the_area_in_the_library_frame(self):
        import pcbnew

        from pnr.ingest import build_graph

        want = sorted((x, -y) for x, y in AREA_KICAD)  # library frame, y up
        with tempfile.TemporaryDirectory() as tmp:
            for side, rot in (("top", 0), ("top", 90), ("bottom", 0), ("bottom", 270)):
                with self.subTest(side=side, rot=rot):
                    graph = build_graph(pcbnew.LoadBoard(str(self.build(Path(tmp), side, rot))))
                    comp = graph.component("J3")
                    self.assertEqual(comp.side, side)
                    (area,) = comp.rule_areas
                    got = sorted((round(x, 6), round(y, 6)) for x, y in area["outline"])
                    self.assertEqual(got, want)
                    self.assertEqual(area["layers"], ["F.Cu"])
                    self.assertEqual(area["layers_bottom"], ["B.Cu"])
                    self.assertEqual(
                        area["items"], ["tracks", "vias", "pads", "pours", "footprints"]
                    )
                    self.assertEqual(graph.component("TP1").rule_areas, [])

    def test_writeback_puts_the_area_where_the_engine_says(self):
        import pcbnew

        from pnr.fixed_block import footprint_keepouts
        from pnr.graph import SIDE_BOTTOM
        from pnr.ingest import build_graph
        from pnr.writeback import apply_placement, to_pcb_nm

        with tempfile.TemporaryDirectory() as tmp:
            source = self.build(Path(tmp))
            graph = build_graph(pcbnew.LoadBoard(str(source)))
            for side, rot in (("top", 90.0), ("bottom", 0.0), ("bottom", 90.0), ("top", 270.0)):
                with self.subTest(side=side, rot=rot):
                    comp = graph.component("J3")
                    # Writeback reads the side; the areas are in the library frame.
                    comp.pos, comp.rot, comp.side = (10.0, 3.0), rot, side
                    board = pcbnew.LoadBoard(str(source))
                    apply_placement(board, graph, width=20.0, height=6.0)
                    fp = board.FindFootprintByReference("J3")
                    self.assertEqual(fp.IsFlipped(), side == SIDE_BOTTOM)
                    (zone,) = list(fp.Zones())
                    chain = zone.Outline().Outline(0)
                    got = sorted(
                        (chain.CPoint(n).x, chain.CPoint(n).y) for n in range(chain.PointCount())
                    )
                    (spec,) = [s for s in footprint_keepouts(graph) if s["owner"] == "J3"]
                    want = sorted(to_pcb_nm(x, y, 6.0) for x, y in spec["polygon"])
                    for (gx, gy), (wx, wy) in zip(got, want):
                        self.assertLessEqual(max(abs(gx - wx), abs(gy - wy)), 2)
                    names = [board.GetLayerName(la) for la in zone.GetLayerSet().CuStack()]
                    self.assertEqual(names, spec["layers"])


@unittest.skipUnless(HAVE_PCBNEW, "requires KiCad's pcbnew")
class NativeReadersTest(unittest.TestCase):
    """The Oracle and the shove world see a footprint's own rule areas."""

    @classmethod
    def setUpClass(cls):
        import sys

        import pcbnew

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        library = write_library(root)
        board = root / "source.kicad_pcb"
        done = subprocess.run(
            [sys.executable, "-c", BUILD, str(library), str(board), "top 0"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if done.returncode:
            raise RuntimeError(done.stderr[-3000:])
        cls.board = pcbnew.LoadBoard(str(board))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rules(self):
        from pnr import fab_profile

        return dict(fab=dict(fab_profile.LEGACY_FAB), net_classes=[], diff_pairs=[])

    def test_oracle_bars_vias_and_tracks_in_the_area(self):
        import pcbnew as k

        from pnr.native_electrical import Oracle

        oracle = Oracle(self.board, self.rules())
        fp = self.board.FindFootprintByReference("J3")
        c = fp.GetPosition()
        inside = (c.x / 1e6, c.y / 1e6 - 1.0)  # KiCad frame: y -2.6..1 about the origin
        outside = (c.x / 1e6 + 3.0, c.y / 1e6)
        self.assertFalse(oracle.via("A", inside, 0.4, 0.2))
        self.assertTrue(oracle.via("A", outside, 0.4, 0.2))
        across = ((inside[0] - 2.0, inside[1]), (inside[0] + 2.0, inside[1]))
        self.assertFalse(oracle.clear("A", k.F_Cu, across[0], across[1], 0.2))
        self.assertTrue(oracle.clear("A", k.B_Cu, across[0], across[1], 0.2))

    def test_shove_world_holds_the_area(self):
        import pcbnew as k

        from pnr.shove.world import World

        fp = self.board.FindFootprintByReference("J3")
        c = fp.GetPosition()
        centre = (c.x / 1e6, c.y / 1e6)
        claim = dict(
            status="routed",
            mode="power",
            policy={},
            tracks=[(k.F_Cu, (centre[0] - 3, centre[1]), (centre[0] + 3, centre[1]), 0.2)],
            banks=[],
        )
        self.board.BuildConnectivity()
        world = World(self.board, self.rules(), "A", claim, [centre], 3.0)
        rules = [p for p in world.prims if str(p.get("label", "")).startswith("rule:")]
        self.assertTrue(rules)
        self.assertTrue(all(p["zone"] == dict(tracks=True, vias=True) for p in rules))


@unittest.skipUnless(HAVE_PCBNEW, "requires KiCad's pcbnew")
class RuleAreaIntrusionTest(unittest.TestCase):
    """pnr.electrical_audit.rule_area_intrusions: the ``pads``/``footprints`` area
    kinds nothing else acts on yet (review finding on #85: fixed_block.py:228) are
    at least surfaced as a quantified warning when a foreign pad lands in one."""

    def board(self, root, tp_xy):
        import pcbnew as k

        library = write_library(root)
        b = k.BOARD()
        b.SetCopperLayerCount(2)

        def nm(v):
            return int(round(v * 1e6))

        def V(x, y):
            return k.VECTOR2I(nm(x), nm(y))

        for a, z in [((0, 0), (20, 8)), ((20, 8), (0, 0))]:  # a loose diagonal is enough
            s = k.PCB_SHAPE(b)
            s.SetShape(k.SHAPE_T_SEGMENT)
            s.SetStart(V(*a))
            s.SetEnd(V(*z))
            s.SetLayer(k.Edge_Cuts)
            s.SetWidth(nm(0.1))
            b.Add(s)
        net = k.NETINFO_ITEM(b, "A")
        b.Add(net)

        def place(name, ref, at):
            fp = k.FootprintLoad(str(library), name)
            fp.SetReference(ref)
            b.Add(fp)
            fp.SetPosition(V(*at))
            for pad in fp.Pads():
                pad.SetNet(net)
            return fp

        place("SOCKET", "J3", (10, 4))  # keep-out (library frame) -> world x 9..11, y 1.4..5.0
        place("TP", "T1", tp_xy)
        return b

    def test_a_foreign_pad_inside_the_area_is_reported(self):
        from pnr.electrical_audit import rule_area_intrusions

        with tempfile.TemporaryDirectory() as tmp:
            got = rule_area_intrusions(self.board(Path(tmp), (10, 2)))  # inside
        self.assertEqual([(g["owner"], g["ref"]) for g in got], [("J3", "T1")])

    def test_a_pad_outside_the_area_is_not_reported(self):
        from pnr.electrical_audit import rule_area_intrusions

        with tempfile.TemporaryDirectory() as tmp:
            got = rule_area_intrusions(self.board(Path(tmp), (10, 7)))  # outside
        self.assertEqual(got, [])


# Ingests the source board and writes the graph (KiCad's Python). argv: board, out json.
INGEST = r"""
import sys
from pnr.ingest import load
open(sys.argv[2], "w").write(load(sys.argv[1]).to_json())
"""


@unittest.skipUnless(
    KICAD_PYTHON and Path(KICAD_PYTHON).exists() and KICAD_CLI and Path(KICAD_CLI).exists(),
    "PNR_KICAD_PYTHON and PNR_KICAD_CLI (headless KiCad) required",
)
class DrcTest(unittest.TestCase):
    """Route, write back and judge: KiCad's DRC finds no track or via in the
    socket's keep-out and no via drill near the shield hole."""

    FINDINGS = ("items_not_allowed", "hole_to_hole")

    def run_case(self, root, drop_areas):
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardGraph
        from pnr.route.detail.router import route_board

        env = dict(
            os.environ,
            HOME=os.environ.get("HOME") or str(root),
            PYTHONPATH=str(PNR_ROOT),
        )
        library = write_library(root)
        source = root / "source.kicad_pcb"
        for args in (
            [KICAD_PYTHON, "-c", BUILD, str(library), str(source), "top 0"],
            [KICAD_PYTHON, "-c", INGEST, str(source), str(root / "graph.json")],
        ):
            done = subprocess.run(args, capture_output=True, text=True, timeout=300, env=env)
            self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        graph = BoardGraph.from_json((root / "graph.json").read_text())
        self.assertTrue(graph.component("J3").rule_areas)
        if drop_areas:
            graph.component("J3").rule_areas = []
        cons = compile_constraints({"board": {"outline": {"w": 20, "h": 6}}}, graph.refs)
        fab = dict(
            track_width_mm=0.15,
            clearance_mm=0.1,
            via_diameter_mm=0.4,
            via_drill_mm=0.2,
            hole_clearance_mm=0.15,
            hole_to_hole_mm=0.25,
            edge_clearance_mm=0.3,
            min_through_drill_mm=0.2,
        )
        rules = {"layers": 2, "fab": fab, "net_classes": []}
        routed = route_board(graph, cons, rules, pitch=0.25)
        self.assertEqual(routed.result.unrouted, [])
        (root / "placed.json").write_text(graph.to_json())
        (root / "rules.json").write_text(json.dumps(rules))
        routes = dict(tracks=routed.tracks, vias=routed.vias, unrouted=[])
        (root / "routes.json").write_text(json.dumps(routes))
        out = root / "routed.kicad_pcb"
        done = subprocess.run(
            [KICAD_PYTHON, "-m", "pnr.writeback", str(source), str(root / "placed.json")]
            + ["--out", str(out), "--rules", str(root / "rules.json")]
            + ["--routes", str(root / "routes.json")],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = root / "drc.json"
        done = subprocess.run(
            [KICAD_CLI, "pcb", "drc", str(out), "--format", "json", "--output", str(report)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        self.assertEqual(done.returncode, 0, (done.stdout + done.stderr)[-3000:])
        findings = json.loads(report.read_text())["violations"]
        return [v for v in findings if v["type"] in self.FINDINGS]

    def test_engine_output_has_no_keepout_or_hole_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self.run_case(Path(tmp), drop_areas=False)
        self.assertEqual(found, [], json.dumps(found[:5], indent=1))

    def test_without_the_areas_the_judge_finds_the_crossing(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = self.run_case(Path(tmp), drop_areas=True)
        self.assertIn("items_not_allowed", {v["type"] for v in found})


if __name__ == "__main__":
    unittest.main()
