"""R2 tests — the routing grid + obstacle + pin-access model (pure, no pcbnew)."""

import os
import unittest

from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.detail.grid import RouteGrid

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "testdata", "splanc_dev")


def _pad(name, net, ox, oy, w=0.4, h=0.4):
    return Pad(name=name, net=net, offset=(ox, oy), size=(w, h))


class GridBasicsTest(unittest.TestCase):
    def test_dims_and_mapping(self):
        g = RouteGrid(10.0, 6.0, pitch=0.5)
        self.assertEqual((g.nx, g.ny), (20, 12))
        self.assertEqual(g.nlayers, 2)
        self.assertEqual(g.cell_of(0.0, 0.0), (0, 0))
        self.assertEqual(g.cell_of(9.99, 5.99), (19, 11))
        cx, cy = g.center_of(0, 0)
        self.assertAlmostEqual(cx, 0.25)
        self.assertAlmostEqual(cy, 0.25)

    def test_edge_inset_uses_actual_outline_for_partial_final_cells(self):
        for width, height in [(70, 55), (55, 70), (10, 10), (9.97, 5.03)]:
            grid = RouteGrid(width, height, 0.35)
            inset = 0.45
            grid.block_edge_inset(inset)
            for j in range(grid.ny):
                for i in range(grid.nx):
                    x, y = grid.center_of(i, j)
                    safe = min(x, y, width - x, height - y) >= inset - 1e-9
                    for layer in range(grid.nlayers):
                        self.assertEqual(grid.passable(layer, i, j, "N"), safe)
                        self.assertEqual(grid.via_passable(layer, i, j, "N"), safe)

    def test_side_layer(self):
        g = RouteGrid(10, 10, 0.5)
        self.assertEqual(g.side_layer("top"), 0)
        self.assertEqual(g.side_layer("bottom"), 1)  # last index

    def test_pad_ownership_and_passability(self):
        g = RouteGrid(10, 10, 0.5)
        from pnr.route.detail.grid import Rect

        g.add_pad(0, "A", Rect(5.0, 5.0, 0.4, 0.4))
        i, j = g.cell_of(5.0, 5.0)
        # own net passes, other net does not
        self.assertTrue(g.passable(0, i, j, "A"))
        self.assertFalse(g.passable(0, i, j, "B"))
        # free cell passes for anyone
        self.assertTrue(g.passable(0, 0, 0, "B"))
        # other layer's cell is free (pad is on layer 0)
        self.assertTrue(g.passable(1, i, j, "B"))

    def test_overlapping_pad_halos_do_not_erase_foreign_clearance(self):
        from pnr.route.detail.grid import Rect

        for order in (("A", "B"), ("B", "A")):
            g = RouteGrid(10, 10, 0.1, clearance=0.15, track_width=0.2, via_radius=0.3)
            pads = {"A": Rect(5, 5, 0.25, 0.8), "B": Rect(5.4, 5, 0.25, 0.8)}
            for net in order:
                g.add_pad(0, net, pads[net])
            i, j = g.cell_of(5.2, 5)
            for net in order:
                self.assertFalse(g.passable(0, i, j, net))
                self.assertFalse(g.via_passable(0, i, j, net))

    def test_block_region_never_routable(self):
        g = RouteGrid(10, 10, 0.5)
        from pnr.route.detail.grid import Rect

        g.block_region(Rect(2.0, 2.0, 1.0, 1.0))
        i, j = g.cell_of(2.0, 2.0)
        self.assertFalse(g.passable(0, i, j, "A"))
        self.assertFalse(g.passable(1, i, j, "A"))

    def test_out_of_bounds(self):
        g = RouteGrid(10, 10, 0.5)
        self.assertFalse(g.passable(0, -1, 0, "A"))
        self.assertFalse(g.passable(0, 999, 0, "A"))


def _segment_rect_distance(a, b, r):
    """Least distance between segment ``a``-``b`` and rectangle ``r`` (0 if they
    meet): for disjoint convex shapes it is reached at a vertex of one of them."""
    import math

    def point_rect(p):
        dx = max(r.left - p[0], 0.0, p[0] - r.right)
        dy = max(r.bottom - p[1], 0.0, p[1] - r.top)
        return math.hypot(dx, dy)

    def point_segment(p):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        return math.hypot(a[0] + t * dx - p[0], a[1] + t * dy - p[1])

    # Liang-Barsky clip: does the segment enter the rectangle at all?
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    meets = True
    for p, q in (
        (-dx, a[0] - r.left),
        (dx, r.right - a[0]),
        (-dy, a[1] - r.bottom),
        (dy, r.top - a[1]),
    ):
        if p == 0:
            if q < 0:
                meets = False
        elif p < 0:
            t0 = max(t0, q / p)
        else:
            t1 = min(t1, q / p)
    if meets and t0 <= t1:
        return 0.0
    corners = [(x, y) for x in (r.left, r.right) for y in (r.bottom, r.top)]
    return min([point_rect(a), point_rect(b)] + [point_segment(c) for c in corners])


class WidePadClearanceTest(unittest.TestCase):
    """A net wider than ``track + pitch`` keeps its copper ``clearance`` from
    foreign pads (the pad track halo is sized for signal tracks)."""

    def _grid(self, rng, width):
        from pnr.route.detail.grid import Rect

        g = RouteGrid(6.0, 6.0, 0.25, clearance=0.2, track_width=0.25)
        for _ in range(6):
            r = Rect(
                rng.uniform(0.5, 5.5),
                rng.uniform(0.5, 5.5),
                rng.uniform(0.2, 1.5),
                rng.uniform(0.2, 1.5),
            )
            g.add_pad(rng.randrange(2), rng.choice(("W", "A", "")), r)
        g.net_widths = {"W": width, "S": 0.25, "M": 0.5}
        g.reserve_wide_pad_clearance()
        return g

    def test_no_table_up_to_track_plus_pitch(self):
        from pnr.route.detail.grid import Rect

        g = RouteGrid(4.0, 4.0, 0.25, clearance=0.2, track_width=0.25)
        g.add_pad(0, "A", Rect(2.0, 2.0, 0.5, 0.5))
        g.net_widths = {"V": 0.4, "W": 0.5}
        g.reserve_wide_pad_clearance()
        self.assertEqual(g.wide_pad_net, {})

    def test_route_edges_keep_wide_copper_clear_of_foreign_pads(self):
        import random

        for seed in range(12):
            rng = random.Random(seed)
            width = rng.choice((0.525, 0.9, 1.3))
            g = self._grid(rng, width)
            self.assertIn("W", g.wide_pad_net)
            self.assertNotIn("M", g.wide_pad_net)
            need = g.clearance + width / 2 - 1e-9

            own = set()
            for la, owner, r in g.pad_rectangles:
                if owner == "W":
                    g._mark_rect(la, r, 0.0, lambda *cell: own.add(cell))

            def free(la, i, j):
                # Passable, and not a cell of the net's own pads (where it lands
                # whatever its neighbours).
                return g.passable(la, i, j, "W") and (la, i, j) not in own

            foreign = [(la, r) for la, owner, r in g.pad_rectangles if owner != "W"]
            for la in range(g.nlayers):
                for j in range(g.ny):
                    for i in range(g.nx):
                        if not free(la, i, j):
                            continue
                        for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
                            if not g.in_bounds(i + di, j + dj) or not free(la, i + di, j + dj):
                                continue
                            if di and dj and not (free(la, i + di, j) and free(la, i, j + dj)):
                                continue
                            a, b = g.center_of(i, j), g.center_of(i + di, j + dj)
                            for pla, r in foreign:
                                if pla == la:
                                    self.assertGreaterEqual(_segment_rect_distance(a, b, r), need)

    def test_own_pads_stay_reachable_and_other_nets_unchanged(self):
        import random

        from pnr.route.detail.grid import Rect

        g = RouteGrid(6.0, 4.0, 0.25, clearance=0.2, track_width=0.25)
        # Two pads 1.2 mm apart: a 0.9 mm track lands on its own pad but never
        # runs through the gap, where a signal track still fits.
        g.add_pad(0, "W", Rect(2.0, 2.0, 1.0, 1.4))
        g.add_pad(0, "A", Rect(4.2, 2.0, 1.0, 1.4))
        g.net_widths = {"W": 0.9}
        g.reserve_wide_pad_clearance()
        self.assertTrue(g.passable(0, *g.cell_of(2.0, 2.0), "W"))
        self.assertTrue(g.passable(0, *g.cell_of(1.0, 2.0), "W"))
        self.assertFalse(g.passable(0, *g.cell_of(3.1, 2.0), "W"))
        self.assertTrue(g.passable(0, *g.cell_of(3.1, 2.0), "S"))
        g = self._grid(random.Random(7), 0.9)
        plain = self._grid(random.Random(7), 0.9)
        plain.wide_pad_net = {}
        for la in range(g.nlayers):
            for j in range(g.ny):
                for i in range(g.nx):
                    for net in ("S", "M", "A"):
                        self.assertEqual(g.passable(la, i, j, net), plain.passable(la, i, j, net))


def _segment_distance(a, b, c, d):
    """Least distance between segments ``a``-``b`` and ``c``-``d`` (neither
    crossing the other; a point when both ends are equal)."""
    import math

    def point_segment(p, u, v):
        dx, dy = v[0] - u[0], v[1] - u[1]
        den = dx * dx + dy * dy
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - u[0]) * dx + (p[1] - u[1]) * dy) / den))
        return math.hypot(u[0] + t * dx - p[0], u[1] + t * dy - p[1])

    return min(
        point_segment(a, c, d),
        point_segment(b, c, d),
        point_segment(c, a, b),
        point_segment(d, a, b),
    )


def _free_edges(g, net, own=()):
    """Every route edge ``net`` may take on ``g`` (orthogonal and 45° steps whose
    cells, and for a step both corner cells, are passable), off the cells ``own``."""

    def free(la, i, j):
        return g.passable(la, i, j, net) and (la, i, j) not in own

    for la in range(g.nlayers):
        for j in range(g.ny):
            for i in range(g.nx):
                if not free(la, i, j):
                    continue
                for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
                    if not g.in_bounds(i + di, j + dj) or not free(la, i + di, j + dj):
                        continue
                    if di and dj and not (free(la, i + di, j) and free(la, i, j + dj)):
                        continue
                    yield la, g.center_of(i, j), g.center_of(i + di, j + dj)


class WideCopperRecordedLaterTest(unittest.TestCase):
    """The wide net's tables also cover copper recorded after they were first
    built (a power array's pads, the escape plan's stubs and vias), at each
    item's own extent, once :meth:`RouteGrid.reserve_wide_pad_clearance` runs
    again, as :func:`pnr.route.detail.maze.route` does before routing."""

    def test_pad_added_after_the_tables(self):
        from pnr.route.detail.grid import Rect
        from pnr.route.detail.maze import route

        for late in (False, True):
            g = RouteGrid(10, 10, 0.25, clearance=0.2, track_width=0.25, via_radius=0.3)
            g.net_widths = {"W": 0.9}
            pad = Rect(5.0, 5.0, 1.0, 1.0)
            if not late:
                g.add_pad(0, "X", pad)
            g.reserve_wide_pad_clearance()
            if late:
                g.add_pad(0, "X", pad)  # e.g. router._mark_source_arrays
                route(g, {})  # brings the tables up to date
            for la, a, b in _free_edges(g, "W"):
                if la == 0:
                    self.assertGreaterEqual(_segment_rect_distance(a, b, pad), 0.2 + 0.45 - 1e-9)

    def test_escape_stubs_and_vias(self):
        import random

        for seed in range(6):
            rng = random.Random(seed)
            width = rng.choice((0.525, 0.9))
            g = RouteGrid(6, 6, 0.25, clearance=0.2, track_width=0.25, via_radius=0.3)
            g.net_widths = {"W": width, "E": rng.choice((0.25, 0.4))}
            g.reserve_wide_pad_clearance()
            stubs = []
            for _ in range(3):
                a = (rng.uniform(1, 5), rng.uniform(1, 5))
                b = (a[0] + rng.uniform(-1, 1), a[1] + rng.uniform(-1, 1))
                la = rng.randrange(2)
                g.escape_segments.append((la, "E", a, b))
                stubs.append((la, a, b))
            via = (rng.uniform(1, 5), rng.uniform(1, 5))
            g.escape_vias.append(("E", via))
            g.reserve_wide_pad_clearance()
            stub = g.net_widths["E"] / 2
            for la, a, b in _free_edges(g, "W"):
                for sla, c, d in stubs:
                    if sla == la:
                        self.assertGreaterEqual(
                            _segment_distance(a, b, c, d), stub + 0.2 + width / 2 - 1e-9
                        )
                self.assertGreaterEqual(
                    _segment_distance(a, b, via, via), 0.3 + 0.2 + width / 2 - 1e-9
                )

    def test_class_clearance_of_either_net(self):
        from pnr.route.detail.grid import Rect

        for owner, mine in ((0.5, None), (None, 0.5)):
            g = RouteGrid(8, 8, 0.25, clearance=0.2, track_width=0.25)
            g.net_widths = {"W": 0.9}
            g.net_clearances = {k: v for k, v in (("X", owner), ("W", mine)) if v}
            pad = Rect(4.0, 4.0, 1.0, 1.0)
            g.add_pad(0, "X", pad)
            g.reserve_wide_pad_clearance()
            for la, a, b in _free_edges(g, "W"):
                if la == 0:
                    self.assertGreaterEqual(_segment_rect_distance(a, b, pad), 0.5 + 0.45 - 1e-9)

    def test_tables_for_a_board_without_wide_nets_stay_empty(self):
        from pnr.route.detail.grid import Rect

        g = RouteGrid(4, 4, 0.25, clearance=0.2, track_width=0.25)
        g.net_widths = {"V": 0.4}
        g.reserve_wide_pad_clearance()
        g.add_pad(0, "A", Rect(2.0, 2.0, 0.5, 0.5))
        g.escape_vias.append(("A", (1.0, 1.0)))
        g.reserve_wide_pad_clearance()
        self.assertEqual(g.wide_pad_net, {})


class GridFromGraphTest(unittest.TestCase):
    def _two_pad_graph(self):
        a = Component(
            "U1", "fp", (2.0, 5.0), 0.0, "top", (1, 1), (1, 1), pads=[_pad("1", "N", 0, 0)]
        )
        b = Component(
            "U2", "fp", (8.0, 5.0), 0.0, "top", (1, 1), (1, 1), pads=[_pad("1", "N", 0, 0)]
        )
        return BoardGraph(
            "t", [a, b], [Net("N", 1, [("U1", "1"), ("U2", "1")])], BoardOutline(10, 10)
        )

    def test_access_points(self):
        g = RouteGrid.from_graph(self._two_pad_graph(), 10, 10, pitch=0.5)
        cells = g.net_access(self._two_pad_graph(), "N")
        self.assertEqual(len(cells), 2)
        self.assertEqual(cells[0].layer, 0)
        # the two pads map to different cells
        self.assertNotEqual((cells[0].i, cells[0].j), (cells[1].i, cells[1].j))

    def test_bottom_pad_on_last_layer(self):
        c = Component(
            "U1", "fp", (5, 5), 0.0, "bottom", (1, 1), (1, 1), pads=[_pad("1", "N", 0, 0)]
        )
        g = RouteGrid.from_graph(BoardGraph("t", [c], [], BoardOutline(10, 10)), 10, 10, pitch=0.5)
        i, j = g.cell_of(5, 5)
        self.assertEqual(g.pad_net.get((1, i, j)), "N")  # layer 1 = bottom
        self.assertIsNone(g.pad_net.get((0, i, j)))


class GridFixtureTest(unittest.TestCase):
    def test_builds_on_real_placed_board(self):
        # Use the frozen (ingested) graph — it now carries pad sizes.
        with open(os.path.join(FIXTURE, "graph.json"), encoding="utf-8") as fh:
            graph = BoardGraph.from_json(fh.read())
        w, h = graph.outline.width, graph.outline.height
        g = RouteGrid.from_graph(graph, w, h, pitch=0.25)
        # Every net's pads resolve to access cells.
        total = sum(len(g.net_access(graph, n.name)) for n in graph.nets)
        self.assertGreater(total, 300)  # ~338 pads
        # Pads are recorded as owned cells.
        self.assertGreater(len(g.pad_net), 300)


class SourceArrayReservationTest(unittest.TestCase):
    def test_future_current_bank_blocks_signals_on_every_crossed_layer(self):
        from test_plane_access_intent import FAB

        from pnr.graph import BoardGraph, BoardOutline, Component, Pad
        from pnr.plane_intent import array_geometry
        from pnr.route.detail.router import _mark_source_arrays

        pads = [Pad(str(i), "return", (1, y), (0.7, 0.5)) for i, y in enumerate((-1, 0, 1), 1)]
        comp = Component("RENAMED", "power", (5, 5), 0, "top", (3, 3), (3, 3), pads=pads)
        graph = BoardGraph("test", [comp], [], BoardOutline(12, 12))
        intent = dict(
            kind="power_array",
            ref=comp.ref,
            pads=["1", "2", "3"],
            net="return",
            surface="F.Cu",
            rms_current_a=5,
            peak_current_a=16,
            max_array_span_mm=3,
        )
        rules = dict(plane_access_intents=[intent], plane_access_fab=FAB)
        grid = RouteGrid(12, 12, 0.1, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
        _mark_source_arrays(grid, graph, rules)
        plan = array_geometry([((6, 5 + y), (0.7, 0.5)) for y in (-1, 0, 1)], (5, 5), intent, FAB)
        for point, diameter, drill in plan["vias"]:
            i, j = grid.cell_of(*point)
            for layer in range(4):
                self.assertFalse(grid.passable(layer, i, j, "signal"))
                self.assertFalse(grid.via_passable(layer, i, j, "signal"))
        self.assertTrue(grid.passable(0, *grid.cell_of(2, 2), "signal"))


if __name__ == "__main__":
    unittest.main()
