"""The board's own outline for the router and writeback (``board.edge: exact``).

A rounded outline drawn at a 0.05 mm stroke (the radar's) is parsed into the engine
frame; the router keeps vias from it as KiCad's hole-to-edge rule measures (to the
stroke's edge), corners included; writeback keeps it instead of stamping the
0.15 mm rectangle.
"""

import math
import unittest

from pnr.board_edge import (
    attach_edges,
    grid_edge_masks,
    keep_outline,
    kept_outline_text,
    outline_text,
    parse_edges,
    via_edge_inset,
)
from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.grid import RouteGrid
from pnr.route.detail.router import route_board

# The radar's Edge.Cuts as gen_board draws it: 60 x 46.3 mm at (30, 30), 1 mm corners.
RADAR = """(kicad_pcb (version 20240108)
\t(gr_line (start 30 31) (end 30 75.3) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_arc (start 30 31) (mid 30.292893 30.292893) (end 31 30)
\t\t(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_arc (start 31 76.3) (mid 30.292893 76.007107) (end 30 75.3)
\t\t(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 31 76.3) (end 89 76.3) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 89 30) (end 31 30) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_arc (start 89 30) (mid 89.707107 30.292893) (end 90 31)
\t\t(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_arc (start 90 75.3) (mid 89.707107 76.007107) (end 89 76.3)
\t\t(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 90 75.3) (end 90 31) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_rect (start 36.8 69.05) (end 52.5 75.9) (stroke (width 0.05)) (layer "F.SilkS"))
)
"""

FAB = dict(
    track_width_mm=0.15,
    clearance_mm=0.1,
    via_diameter_mm=0.4,
    via_drill_mm=0.2,
    hole_clearance_mm=0.15,
    edge_clearance_mm=0.3,
    hole_to_edge_mm=0.5,
)


class ParseTest(unittest.TestCase):
    def test_radar_outline(self):
        edges = parse_edges(RADAR)
        self.assertEqual(edges["size"], [60.0, 46.3])
        self.assertEqual(edges["origin"], [30.0, 30.0])
        self.assertEqual(edges["stroke_mm"], 0.05)
        kinds = sorted(it["kind"] for it in edges["items"])
        self.assertEqual(kinds, ["arc"] * 4 + ["line"] * 4)
        # Engine frame: y up, origin at the bottom-left.
        line = next(it for it in edges["items"] if it["kind"] == "line")
        self.assertEqual(line["a"], [0.0, 45.3])
        self.assertIsNone(parse_edges("(kicad_pcb)"))

    def test_attach_only_when_exact(self):
        rules = {}
        attach_edges(rules, RADAR)
        self.assertNotIn("board_edges", rules)
        rules = {"edge": "exact"}
        attach_edges(rules, RADAR)
        self.assertEqual(rules["board_edges"]["size"], [60.0, 46.3])
        self.assertTrue(keep_outline(rules))
        self.assertTrue(keep_outline({"keep_outline": True}))
        self.assertFalse(keep_outline({}))


class InsetTest(unittest.TestCase):
    def test_trial_two_via(self):
        # 0.625 mm from the centre line: 0.5 from the stroke edge of a 0.05 outline
        # less the 0.1 drill radius, clear of the 0.475 rule; with the 0.15 stroke
        # writeback stamped, 0.45: the two trial-2 hole_to_edge findings.
        thin = via_edge_inset(
            {"fab": FAB, "dru": {"hole_to_edge_mm": 0.475}}, {"stroke_mm": 0.05}, 0.2, 0.2
        )
        wide = via_edge_inset(
            {"fab": FAB, "dru": {"hole_to_edge_mm": 0.475}}, {"stroke_mm": 0.15}, 0.2, 0.2
        )
        self.assertLess(thin, 0.625)
        self.assertGreater(wide, 0.625)
        # Without the board's rule: the physical 0.5 mm from the centre line.
        self.assertAlmostEqual(via_edge_inset({"fab": FAB}, {"stroke_mm": 0.15}, 0.2, 0.2), 0.601)

    def test_notch_and_cutout(self):
        # A 10 x 6 mm board with a 2 x 2 mm notch in its top edge and a 1 mm round
        # cut-out: cells in either are outside the board.
        lines = [(0, 0, 10, 0), (10, 0, 10, 6), (10, 6, 6, 6), (6, 6, 6, 4), (6, 4, 4, 4)]
        lines += [(4, 4, 4, 6), (4, 6, 0, 6), (0, 6, 0, 0)]
        text = "(kicad_pcb\n"
        for x0, y0, x1, y1 in lines:  # pcbnew y down
            text += (
                '(gr_line (start %g %g) (end %g %g) (stroke (width 0.1)) (layer "Edge.Cuts"))\n'
                % (
                    30 + x0,
                    36 - y0,
                    30 + x1,
                    36 - y1,
                )
            )
        text += (
            '(gr_circle (center 32 32) (end 32.5 32) (stroke (width 0.1)) (layer "Edge.Cuts")))\n'
        )
        edges = parse_edges(text)
        self.assertEqual(edges["size"], [10.0, 6.0])
        grid = RouteGrid(10, 6, 0.25)
        outside, dist = grid_edge_masks(grid, edges)
        i, j = grid.cell_of(5.0, 5.0)  # in the notch
        self.assertTrue(outside[j, i])
        i, j = grid.cell_of(2.0, 4.0)  # in the cut-out (pcbnew (32, 32) is engine (2, 4))
        self.assertTrue(outside[j, i])
        i, j = grid.cell_of(5.0, 3.0)  # below the notch, about 1 mm from its floor
        self.assertFalse(outside[j, i])
        self.assertAlmostEqual(dist[j, i], 4.0 - 3.125, places=6)

    def test_rounded_corner_distance(self):
        grid = RouteGrid(60, 46.3, 0.25)
        outside, dist = grid_edge_masks(grid, parse_edges(RADAR))
        # Cell (2, 2): centre (0.625, 0.625), 1 - |(0.375, 0.375)| = 0.470 from the arc.
        self.assertAlmostEqual(dist[2, 2], 1 - math.hypot(0.375, 0.375), places=6)
        self.assertFalse(outside[2, 2])
        # Cell (0, 0) lies outside the rounded corner; (40, 2) is 0.625 from the edge.
        self.assertTrue(outside[0, 0])
        self.assertAlmostEqual(dist[2, 40], 0.625, places=6)
        self.assertFalse(outside[90, 120])


def _part(ref, x, y, net):
    return Component(
        ref=ref,
        footprint="test:" + ref,
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(0.6, 0.6),
        bbox=(0.6, 0.6),
        pads=[Pad(name="1", net=net, offset=(0.0, 0.0), size=(0.4, 0.4))],
    )


def corner_board():
    """Net A from the bottom edge to the left edge past the rounded corner of a
    6 x 6 mm board, with its pads on opposite layers (it needs a via)."""
    a = _part("TP1", 3.0, 0.9, "A")
    b = _part("TP2", 0.9, 3.0, "A")
    b.side = "bottom"
    return BoardGraph(
        name="corner",
        components=[a, b],
        nets=[Net(name="A", code=1, pins=[("TP1", "1"), ("TP2", "1")])],
    )


class RouteTest(unittest.TestCase):
    def _route(self, exact):
        graph = corner_board()
        board = {"outline": {"w": 6, "h": 6}}
        if exact:
            board["edge"] = "exact"
        compiled = compile_constraints({"board": board}, graph.refs)
        rules = compile_routing_rules(compiled, ["A"])
        rules["fab"] = dict(FAB)
        text = "(kicad_pcb\n" + outline_text(6, 6, radius=2.0, stroke=0.15) + ")\n"
        attach_edges(rules, text)
        return route_board(graph, compiled, rules, pitch=0.25), rules

    def test_vias_keep_the_rounded_edge(self):
        board, rules = self._route(True)
        report = board.escape_diagnostics["board_edge"]
        self.assertEqual(report["model"], "exact")
        self.assertEqual(board.result.unrouted, [])
        edges = rules["board_edges"]
        for net, x, y in board.vias:
            d = _distance_to_outline(edges, (x, y))
            self.assertGreaterEqual(d, report["via_inset_mm"] - 1e-9, (x, y, d))
        self.assertTrue(board.vias)

    def test_off_has_no_report(self):
        board, _ = self._route(False)
        self.assertNotIn("board_edge", board.escape_diagnostics)

    def test_outline_of_another_size_falls_back(self):
        graph = corner_board()
        compiled = compile_constraints({"board": {"outline": {"w": 6, "h": 6}}}, graph.refs)
        rules = dict(compile_routing_rules(compiled, ["A"]), fab=dict(FAB), edge="exact")
        attach_edges(rules, "(kicad_pcb\n" + outline_text(7, 6, radius=1.0) + ")\n")
        board = route_board(graph, compiled, rules, pitch=0.25)
        self.assertEqual(board.escape_diagnostics["board_edge"]["model"], "rectangle")


def _distance_to_outline(edges, p):
    import numpy as np

    class One:
        nx = ny = 1
        pitch = 1.0

    grid = One()
    # grid_edge_masks samples cell centres; shift the one cell so its centre is p.
    shifted = dict(edges, items=[_shift(it, 0.5 - p[0], 0.5 - p[1]) for it in edges["items"]])
    _outside, dist = grid_edge_masks(grid, shifted)
    return float(np.asarray(dist)[0, 0])


def _shift(it, dx, dy):
    out = dict(it)
    for key in ("a", "b", "m", "c"):
        if key in it:
            out[key] = [it[key][0] + dx, it[key][1] + dy]
    return out


class WritebackTextTest(unittest.TestCase):
    def test_outline_moves_to_the_frame(self):
        self.assertEqual(kept_outline_text(RADAR, 60.0, 46.3), RADAR)  # already in place
        edges = parse_edges(kept_outline_text(_offset(RADAR, 10.0, 5.0), 60.0, 46.3))
        self.assertEqual(edges["origin"], [30.0, 30.0])
        self.assertEqual(edges["size"], [60.0, 46.3])
        # Silkscreen is left alone.
        self.assertIn(
            "(gr_rect (start 36.8 69.05)", kept_outline_text(_offset(RADAR, 10, 5), 60, 46.3)
        )

    def test_other_size_is_refused(self):
        self.assertIsNone(kept_outline_text(RADAR, 60.0, 47.35))
        self.assertIsNone(kept_outline_text("(kicad_pcb)", 60.0, 46.3))

    def test_rounded_outline_text(self):
        edges = parse_edges("(kicad_pcb\n" + outline_text(46, 34, radius=1.0, stroke=0.15) + ")")
        self.assertEqual(edges["size"], [46.0, 34.0])
        self.assertEqual(edges["stroke_mm"], 0.15)
        self.assertEqual(len(edges["items"]), 8)
        square = parse_edges("(kicad_pcb\n" + outline_text(10, 5) + ")")
        self.assertEqual(len(square["items"]), 4)


def _offset(text, dx, dy):
    """``text`` with the Edge.Cuts points moved by (dx, dy) (test helper)."""
    import re

    def move(m):
        return "(%s %g %g)" % (m.group(1), float(m.group(2)) + dx, float(m.group(3)) + dy)

    parts = re.split(r'(\(gr_\w+.*?\(layer "[^"]+"\)\))', text, flags=re.S)
    return "".join(
        re.sub(r"\((start|end|mid)\s+([-\d.]+)\s+([-\d.]+)\)", move, p) if "Edge.Cuts" in p else p
        for p in parts
    )


if __name__ == "__main__":
    unittest.main()
