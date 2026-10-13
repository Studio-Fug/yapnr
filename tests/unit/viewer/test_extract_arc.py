"""The KiCad geometry extractor's arc sweep works on KiCad 10's PCB_ARC API (no KiCad needed)."""

import hashlib
import importlib.util
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from yapnr.viewer import server


def load_extract():
    spec = importlib.util.spec_from_file_location("viewer_extract", server.EXTRACT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Angle:
    def __init__(self, radians):
        self.radians = radians

    def AsRadians(self):
        return self.radians


class Kicad10Arc:
    """KiCad 10: PCB_ARC has GetAngle() and no GetArcAngle()."""

    def GetAngle(self):
        return Angle(-math.pi / 2)


class OlderArc:
    def GetArcAngle(self):
        return Angle(math.pi / 3)


class ArcSweepTest(unittest.TestCase):
    def test_old_geometry_cache_upgrades_only_from_matching_native_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "geometry").mkdir()
            board = root / "board.kicad_pcb"
            board.write_bytes(b"native-board")
            sha = hashlib.sha256(board.read_bytes()).hexdigest()
            cached = root / "geometry" / (sha + ".json")
            old = {"tracks": []}
            updated = dict(old, copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], airwires=[])
            viewer = object.__new__(server.Viewer)
            viewer.root, viewer.cache, viewer.last_sweep = root, {}, time.time()
            viewer.toolchain = SimpleNamespace(python=lambda: ("native-python", None))
            calls = []

            def extract(board, out, log):
                calls.append(board)
                out.write_text(json.dumps(updated))

            viewer.extract = extract
            event = dict(board=str(board), board_sha256=sha)
            cached.write_text(json.dumps(old))
            self.assertEqual(viewer.geometry(event), updated)
            self.assertEqual(len(calls), 1)
            self.assertEqual(viewer.geometry(event), updated)
            self.assertEqual(len(calls), 1)
            # A cache with the stack but without native connectivity also upgrades.
            viewer.cache.clear()
            cached.write_text(json.dumps(dict(old, copper_layers=updated["copper_layers"])))
            self.assertEqual(viewer.geometry(event), updated)
            self.assertEqual(len(calls), 2)
            viewer.cache.clear()
            cached.write_text(json.dumps(old))
            board.write_bytes(b"different-revision")
            self.assertEqual(viewer.geometry(event), old)
            self.assertEqual(len(calls), 2)

    def test_airwires_contract_connected_islands_and_choose_minimum_links(self):
        extract = load_extract()
        self.assertEqual(extract.minimum_airwires([]), [])
        self.assertEqual(extract.minimum_airwires([[(0, 0), (20, 0)]]), [])
        # First group is a long routed branch: choose its near end, not its pad.
        groups = [[(0, 0), (20, 0)], [(21, 0)], [(23, 0)]]
        wires = extract.minimum_airwires(groups)
        self.assertEqual(wires, [[[20, 0], [21, 0]], [[21, 0], [23, 0]]])
        self.assertEqual(sum(math.dist(a, b) for a, b in wires), 3)

    def test_coincident_disconnected_layers_still_have_an_airwire(self):
        self.assertEqual(
            load_extract().minimum_airwires([[(0, 0)], [(0, 0)]]),
            [[[0, 0], [0, 0]]],
        )

    def test_native_connectivity_decides_groups_not_net_name_or_xy(self):
        def item(uid, net, x, kind="PAD"):
            return SimpleNamespace(
                m_Uuid=SimpleNamespace(AsString=lambda: uid),
                GetNetCode=lambda: 1 if net else 0,
                GetNetname=lambda: net,
                GetClass=lambda: kind,
                GetPosition=lambda: (x, 0),
                GetStart=lambda: (x, 0),
                GetEnd=lambda: (x + 9, 0),
            )

        a, b, c = [item(uid, "power", x) for uid, x in [("a", 0), ("b", 5), ("c", 10)]]
        track = item("track", "power", 0, "PCB_TRACK")
        no_net = item("npth", "", 0)
        built = []
        # a/b share copper; c shares the name but has no copper connection.
        conn = SimpleNamespace(
            Build=lambda board: built.append(board),
            GetConnectedItems=lambda p: [a, b, track] if p in (a, b) else [],
        )
        board = SimpleNamespace(
            GetConnectivity=lambda: conn,
            GetFootprints=lambda: [SimpleNamespace(Pads=lambda: [c, b, a, no_net])],
        )
        self.assertEqual(
            load_extract().airwires_of(board, list),
            [dict(net="power", a=[9, 0], b=[10, 0])],
        )
        self.assertEqual(built, [board])

    def test_empty_copper_layers_follow_native_stack_not_numeric_layer_ids(self):
        # KiCad 10's B.Cu ID is 2, before the internal layers numerically.
        names = {0: "F.Cu", 2: "B.Cu", 4: "In1.Cu", 6: "In2.Cu", 8: "In3.Cu", 10: "In4.Cu"}
        k = SimpleNamespace(BOARD=SimpleNamespace(GetStandardLayerName=names.__getitem__))
        board = SimpleNamespace(
            GetEnabledLayers=lambda: SimpleNamespace(CuStack=lambda: [0, 4, 6, 8, 10, 2])
        )
        self.assertEqual(
            load_extract().copper_layers(k, board),
            ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"],
        )

    def test_missing_outline_uses_display_bounds_without_claiming_a_board_outline(self):
        class Box:
            def GetWidth(self):
                return 68000000

            def GetHeight(self):
                return 8000000

        class Board:
            def ComputeBoundingBox(self, include_edges):
                self.include_edges = include_edges
                return Box()

        board = Board()
        self.assertEqual(load_extract().display_bounds(board, None), (68.0, 8.0, False))
        self.assertFalse(board.include_edges)

    def test_kicad10_get_angle(self):
        self.assertAlmostEqual(load_extract().arc_sweep(Kicad10Arc()), -math.pi / 2)

    def test_fallback_get_arc_angle(self):
        self.assertAlmostEqual(load_extract().arc_sweep(OlderArc()), math.pi / 3)


if __name__ == "__main__":
    unittest.main()
