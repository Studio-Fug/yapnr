"""Pad-local clearance and solder mask margin (a fiducial's): ingested onto the graph,
kept by the routing grid's halos, the exact escape checks and the maze.

A pad that sets its own clearance (KiCad's local override) or a solder mask margin
asks foreign copper to keep the larger of the two away: the clearance by KiCad's
clearance rule, the margin because foreign copper inside the mask aperture is
exposed beside the pad (``solder_mask_bridge``). Pads without either are unchanged.
"""

import json
import math
import unittest

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.grid import RouteGrid, pad_keepaway
from pnr.route.detail.router import route_board
from pnr.writeback import _segment_distance_sq

FAB = dict(
    track_width_mm=0.15,
    clearance_mm=0.1,
    via_diameter_mm=0.4,
    via_drill_mm=0.2,
    hole_clearance_mm=0.15,
)


def _part(ref, x, y, net, size=0.6, **local):
    return Component(
        ref=ref,
        footprint="test:" + ref,
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(size + 0.2, size + 0.2),
        bbox=(size + 0.2, size + 0.2),
        pads=[Pad(name="1", net=net, offset=(0.0, 0.0), size=(size, size), **local)],
    )


def fiducial_board(local=True):
    """Net A from TP1 to TP2 straight through a 1 mm fiducial (FID1, no net) with a
    0.6 mm clearance and a 0.5 mm mask margin, on a 20 x 6 mm two-layer board."""
    fid = dict(clearance_mm=0.6, mask_margin_mm=0.5) if local else {}
    parts = [
        _part("TP1", 3.0, 3.0, "A"),
        _part("TP2", 17.0, 3.0, "A"),
        _part("FID1", 10.0, 3.0, "", size=1.0, **fid),
    ]
    nets = [Net(name="A", code=1, pins=[("TP1", "1"), ("TP2", "1")])]
    return BoardGraph(name="fid", components=parts, nets=nets)


def _constraints():
    return compile_constraints({"board": {"outline": {"w": 20, "h": 6}}}, ["TP1", "TP2", "FID1"])


def _rules():
    return {"layers": 2, "fab": dict(FAB), "net_classes": []}


def _closest_to_fiducial(board):
    """Least distance (mm) from a net-A track centreline to FID1's pad copper."""
    left, right, bottom, top = 9.5, 10.5, 2.5, 3.5
    corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    best = math.inf
    for net, layer, a, b, _w in board.tracks:
        if layer != "F.Cu":
            continue
        for k in range(4):
            best = min(
                best, math.sqrt(_segment_distance_sq(a, b, corners[k], corners[(k + 1) % 4]))
            )
    return best


class KeepawayTest(unittest.TestCase):
    def test_larger_of_clearance_and_mask_margin(self):
        self.assertEqual(pad_keepaway(Pad("1", "", (0, 0), clearance_mm=0.6)), 0.6)
        self.assertAlmostEqual(pad_keepaway(Pad("1", "", (0, 0), mask_margin_mm=0.5)), 0.501)
        both = Pad("1", "", (0, 0), clearance_mm=0.3, mask_margin_mm=0.5)
        self.assertAlmostEqual(pad_keepaway(both), 0.501)
        self.assertIsNone(pad_keepaway(Pad("1", "A", (0, 0))))

    def test_graph_carries_the_keys_only_when_set(self):
        graph = fiducial_board()
        text = graph.to_json()
        pads = {c.ref: c.pads[0] for c in BoardGraph.from_json(text).components}
        self.assertEqual(pads["FID1"].clearance_mm, 0.6)
        self.assertEqual(pads["FID1"].mask_margin_mm, 0.5)
        plain = json.loads(fiducial_board(local=False).to_json())
        for comp in plain["components"]:
            self.assertNotIn("clearance_mm", comp["pads"][0])
            self.assertNotIn("mask_margin_mm", comp["pads"][0])
        self.assertIsNone(pads["TP1"].clearance_mm)


class GridHaloTest(unittest.TestCase):
    def _grid(self, local):
        return RouteGrid.from_graph(
            fiducial_board(local),
            20,
            6,
            pitch=0.25,
            clearance=0.1,
            track_width=0.15,
            via_radius=0.2,
        )

    def test_track_and_via_halos_grow_by_the_pad_clearance(self):
        grid = self._grid(True)
        plain = self._grid(False)
        # A cell whose centre is 0.5 mm right of the pad's edge: open to a track by
        # the fab clearance, closed by the fiducial's 0.6 mm.
        i, j = grid.cell_of(10.5 + 0.5, 3.0)
        self.assertTrue(plain.passable(0, i, j, "A"))
        self.assertFalse(grid.passable(0, i, j, "A"))
        # 0.75 mm out a via (0.2 radius) is too close as well; 1.2 mm out it is not.
        i, j = grid.cell_of(10.5 + 0.75, 3.0)
        self.assertTrue(plain.via_passable(0, i, j, "A"))
        self.assertFalse(grid.via_passable(0, i, j, "A"))
        i, j = grid.cell_of(10.5 + 1.2, 3.0)
        self.assertTrue(grid.via_passable(0, i, j, "A"))
        self.assertEqual(len(grid.pad_keepaways), 1)
        self.assertEqual(plain.pad_keepaways, {})

    def test_exact_escape_check_refuses_the_aperture(self):
        from pnr.route.detail.joint_escape import _segment_clear

        grid = self._grid(True)
        plain = self._grid(False)
        # A stub 0.4 mm above the fiducial: inside its 0.6 mm clearance.
        a, b = (9.0, 3.9 + 0.075), (11.0, 3.9 + 0.075)
        self.assertTrue(_segment_clear(plain, "A", 0, a, b, 0.15, offsets=False))
        self.assertFalse(_segment_clear(grid, "A", 0, a, b, 0.15, offsets=False))

    def test_fanout_pad_check_reads_the_keepaway(self):
        from pnr.route.detail.fanout import _clear_of_pads

        grid = self._grid(True)
        pads = list(grid.pad_rectangles)
        a, b = (9.0, 3.95), (11.0, 3.95)
        self.assertTrue(_clear_of_pads(pads, "A", 0, a, b, 0.15, 0.1, {}))
        self.assertFalse(_clear_of_pads(pads, "A", 0, a, b, 0.15, 0.1, {}, grid.pad_keepaways))


def thermal_land_board(far=True):
    """U1 (top) with an exposed pad (GND) repeated as a 1.5 x 1.75 mm land on B.Cu
    (``far_side``), and net A from TP1 to TP2 on the bottom side straight under it."""
    u1 = Component(
        ref="U1",
        footprint="test:U1",
        pos=(10.0, 3.0),
        rot=0.0,
        side="top",
        courtyard=(2.0, 2.2),
        bbox=(2.0, 2.2),
        pads=[
            Pad(name="9", net="GND", offset=(0.0, 0.0), size=(1.5, 1.75), land_corner=0.0),
            Pad(
                name="9",
                net="GND",
                offset=(0.0, 0.0),
                size=(1.5, 1.75),
                land_corner=0.0,
                far_side=True if far else None,
            ),
        ],
    )
    parts = [_part("TP1", 3.0, 3.0, "A"), _part("TP2", 17.0, 3.0, "A"), u1]
    for tp in parts[:2]:
        tp.side = "bottom"
    nets = [
        Net(name="A", code=1, pins=[("TP1", "1"), ("TP2", "1")]),
        Net(name="GND", code=2, pins=[("U1", "9")]),
    ]
    return BoardGraph(name="land", components=parts, nets=nets)


class FarSideLandTest(unittest.TestCase):
    def test_the_land_bars_its_own_layer(self):
        def grid(far):
            return RouteGrid.from_graph(
                thermal_land_board(far),
                20,
                6,
                pitch=0.25,
                clearance=0.1,
                track_width=0.15,
                via_radius=0.2,
            )

        i, j = grid(True).cell_of(10.0, 3.0)
        self.assertFalse(grid(True).passable(1, i, j, "A"))  # B.Cu under the land
        self.assertTrue(grid(False).passable(1, i, j, "A"))  # read as a second top land
        self.assertFalse(grid(True).passable(0, i, j, "A"))  # the top land stays
        layers = sorted({la for la, net, _r, _land in grid(True).smd_pads if net == "GND"})
        self.assertEqual(layers, [0, 1])
        self.assertEqual(grid(True).access[("GND", "U1.9")].layer, 0)

    def test_graph_carries_the_key_only_when_set(self):
        text = thermal_land_board().to_json()
        pads = BoardGraph.from_json(text).component("U1").pads
        self.assertEqual([p.far_side for p in pads], [None, True])
        self.assertEqual(json.loads(text)["components"][2]["pads"][0].get("far_side", "-"), "-")

    def test_route_keeps_off_the_land(self):
        board = route_board(thermal_land_board(), _constraints(), _rules(), pitch=0.25)
        self.assertEqual(board.result.unrouted, [])
        for net, layer, a, b, w in board.tracks:
            if net == "A" and layer == "B.Cu":
                d = math.sqrt(_segment_distance_sq(a, b, (10.0, 3.0), (10.0, 3.0)))
                self.assertGreaterEqual(d, 0.875 + w / 2 + 0.1 - 1e-6)
        # Read as a second top land (no key), the bottom route crosses under it.
        legacy = route_board(thermal_land_board(False), _constraints(), _rules(), pitch=0.25)
        crossing = [
            math.sqrt(_segment_distance_sq(a, b, (10.0, 3.0), (10.0, 3.0)))
            for net, layer, a, b, _w in legacy.tracks
            if net == "A" and layer == "B.Cu"
        ]
        self.assertLess(min(crossing), 0.875)


class RouteTest(unittest.TestCase):
    def test_route_keeps_the_fiducial_clearance(self):
        board = route_board(fiducial_board(True), _constraints(), _rules(), pitch=0.25)
        self.assertEqual(board.result.unrouted, [])
        self.assertGreaterEqual(_closest_to_fiducial(board), 0.6 + 0.075 - 1e-6)

    def test_without_local_values_the_route_is_unchanged(self):
        # The fab clearance alone lets the route pass closer than the fiducial asks.
        board = route_board(fiducial_board(False), _constraints(), _rules(), pitch=0.25)
        self.assertEqual(board.result.unrouted, [])
        self.assertLess(_closest_to_fiducial(board), 0.6)


if __name__ == "__main__":
    unittest.main()
