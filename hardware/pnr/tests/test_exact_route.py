"""Exact-separation routing: the separation model, its zones and the recovery mode."""

import math
import os
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from pnr.route.detail import native_maze
from pnr.route.detail.exact_route import (
    Occupancy,
    Separation,
    Zone,
    missing_connections,
    route_exact,
)
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.maze import _Route, route
from pnr.writeback import _segment_distance_sq


def native_kernel(test):
    """The native kernel, compiled with the host compiler when Bazel did not
    provide it; the test is skipped without one."""
    native_maze.reset()
    test.addCleanup(native_maze.reset)
    with patch.dict(os.environ, {"PNR_MAZE_KERNEL": "native"}):
        kernel = native_maze.load()
    if kernel is None and shutil.which(os.environ.get("CC", "cc")):
        out = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, out, True)
        library = native_maze.build_library(out)
        native_maze.reset()
        with patch.dict(os.environ, {"PNR_MAZE_LIB": str(library)}):
            kernel = native_maze.load()
    if kernel is None:
        test.skipTest("native maze library unavailable: " + native_maze.status()["reason"])
    return kernel


def board(pitch=0.25, nx=40, ny=30, layers=("F.Cu", "B.Cu"), widths=None):
    """A grid with the ladder's fab rules (0.25 mm tracks, 0.2 mm clearance, 0.6/0.3 mm
    vias, 0.25 mm hole spacing)."""
    grid = RouteGrid(
        nx * pitch,
        ny * pitch,
        pitch,
        layers=layers,
        clearance=0.2,
        track_width=0.25,
        via_radius=0.3,
    )
    grid.via_spacing = 0.3 + 0.25
    grid.via_drill_radius = 0.15
    grid.net_widths = dict(widths or {})
    return grid


def line(layer, cells):
    cells = [Cell(layer, i, j) for i, j in cells]
    return _Route(list(cells), list(zip(cells, cells[1:])))


def via(i, j, layers=2):
    cells = [Cell(la, i, j) for la in range(layers)]
    return _Route(cells, list(zip(cells, cells[1:])))


def conflict(grid, a, b, nets=("A", "B")):
    sep = Separation(grid, nets)
    occ = Occupancy(grid, sep)
    occ.add(Zone(grid, sep, nets[1], b))
    zone = Zone(grid, sep, nets[0], a)
    result = occ.conflicts(zone)
    # Symmetric, and the crossed set agrees.
    other = Occupancy(grid, sep)
    other.add(Zone(grid, sep, nets[0], a))
    assert other.conflicts(Zone(grid, sep, nets[1], b)) == result
    assert (occ.crossed(zone) == [nets[1]]) == result
    return result


def copper_gaps(grid, result, widths):
    """Smallest copper gap between two different nets' routed tracks and vias (mm)."""
    items = []
    for net, rn in result.nets.items():
        r = widths.get(net, grid.track_width) / 2
        for layer, a, b in rn.segments:
            items.append((net, layer, grid.center_of(*a), grid.center_of(*b), r))
        for i, j in rn.vias:
            p = grid.center_of(i, j)
            for layer in range(grid.nlayers):
                items.append((net, layer, p, p, grid.via_radius))
    worst = math.inf
    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            a, b = items[x], items[y]
            if a[0] == b[0] or a[1] != b[1]:
                continue
            d = math.sqrt(_segment_distance_sq(a[2], a[3], b[2], b[3])) - a[4] - b[4]
            worst = min(worst, d)
    return worst


class SeparationTest(unittest.TestCase):
    def test_distances_and_stencils_at_the_ladder_rules(self):
        grid = board(widths={"W": 0.4})
        sep = Separation(grid, ["S", "W"])
        s, w, v = sep.kind["S"], sep.kind["W"], sep.via
        self.assertAlmostEqual(sep.distance[s][s], 0.45)
        self.assertAlmostEqual(sep.distance[w][s], 0.525)
        self.assertAlmostEqual(sep.distance[v][s], 0.625)
        self.assertAlmostEqual(sep.distance[v][v], 0.8)

        def inside(stencil, dj, di):
            return any(int(a) == dj and int(b) == di for a, b in zip(*stencil))

        cases = [
            (s, s, (0, 2), False),
            (s, s, (0, 1), True),
            (s, s, (1, 1), True),
            (s, s, (1, 2), False),
            (w, s, (0, 2), True),
            (w, s, (1, 2), False),
            (w, s, (0, 3), False),
            (v, s, (0, 2), True),
            (v, s, (1, 2), True),
            (v, s, (2, 2), False),
            (v, s, (0, 3), False),
            (v, v, (0, 3), True),
            (v, v, (2, 3), False),
            (v, v, (0, 4), False),
        ]
        for e, k, (dj, di), expected in cases:
            with self.subTest(e=e, k=k, offset=(dj, di)):
                self.assertEqual(inside(sep.cc[e][k], dj, di), expected)
                self.assertEqual(inside(sep.cc[k][e], dj, di), expected)

    def test_hole_spacing_floors_via_to_via(self):
        grid = board()
        grid.via_spacing = 1.3
        sep = Separation(grid, ["S"])
        self.assertAlmostEqual(sep.distance[sep.via][sep.via], 1.3)


class ZoneTest(unittest.TestCase):
    def test_parallel_tracks(self):
        grid = board()
        a = line(0, [(i, 10) for i in range(5, 20)])
        self.assertFalse(conflict(grid, a, line(0, [(i, 12) for i in range(5, 20)])))
        self.assertTrue(conflict(grid, a, line(0, [(i, 11) for i in range(5, 20)])))
        self.assertFalse(conflict(grid, a, line(1, [(i, 10) for i in range(5, 20)])))

    def test_parallel_diagonals(self):
        grid = board()
        a = line(0, [(5 + k, 5 + k) for k in range(10)])
        # Three cells apart along a row: 3/sqrt(2) pitches = 0.53 mm >= 0.45.
        self.assertFalse(conflict(grid, a, line(0, [(8 + k, 5 + k) for k in range(10)])))
        self.assertTrue(conflict(grid, a, line(0, [(7 + k, 5 + k) for k in range(10)])))

    def test_crossing_steps_meet_at_their_centre(self):
        # A coarse grid: the ends are a full pitch apart, the steps still cross.
        grid = board(pitch=1.0, nx=10, ny=10)
        grid.clearance = 0.1
        grid.track_width = 0.1
        grid.via_radius = 0.1
        grid.via_spacing = 0.3
        self.assertTrue(conflict(grid, line(0, [(2, 2), (3, 3)]), line(0, [(3, 2), (2, 3)])))
        self.assertFalse(conflict(grid, line(0, [(2, 2), (3, 3)]), line(0, [(4, 2), (3, 3 - 2)])))

    def test_step_centre_against_a_cell(self):
        grid = board()
        step = line(0, [(10, 10), (11, 11)])
        # Cell (12, 9): 2.12 pitches = 0.53 mm from the step's centre, 2.24 from its ends.
        self.assertFalse(conflict(grid, step, line(0, [(12, 9), (13, 9)])))
        # Cell (12, 10): 1.58 pitches = 0.40 mm from the centre: too close.
        self.assertTrue(conflict(grid, step, line(0, [(12, 10), (13, 10)])))

    def test_via_to_track_and_via(self):
        grid = board()
        v = via(10, 10)
        self.assertFalse(conflict(grid, v, line(0, [(13, j) for j in range(5, 15)])))
        self.assertTrue(conflict(grid, v, line(1, [(12, j) for j in range(5, 15)])))
        self.assertFalse(conflict(grid, v, via(14, 10)))
        self.assertTrue(conflict(grid, v, via(13, 10)))

    def test_wide_track(self):
        grid = board(widths={"A": 0.4})
        a = line(0, [(i, 10) for i in range(5, 20)])
        self.assertFalse(conflict(grid, a, line(0, [(i, 13) for i in range(5, 20)])))
        self.assertTrue(conflict(grid, a, line(0, [(i, 12) for i in range(5, 20)])))


def random_board(seed):
    rng = random.Random(seed)
    layers = ("F.Cu", "B.Cu") if rng.random() < 0.7 else ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    names = ["N%d" % k for k in range(rng.randrange(4, 9))]
    widths = {n: 0.4 for n in names if rng.random() < 0.25}
    grid = board(nx=rng.randrange(24, 36), ny=rng.randrange(18, 28), layers=layers, widths=widths)
    for _ in range(rng.randrange(0, 12)):
        grid.blocked[
            rng.randrange(grid.nlayers), rng.randrange(grid.ny), rng.randrange(grid.nx)
        ] = True
    access = {
        n: [
            Cell(
                rng.randrange(grid.nlayers),
                rng.randrange(2, grid.nx - 2),
                rng.randrange(2, grid.ny - 2),
            )
            for _ in range(rng.randrange(2, 5))
        ]
        for n in names
    }
    return grid, access, widths


class ExactRouteTest(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1"})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_routes_keep_the_exact_copper_clearance(self):
        for seed in range(25):
            with self.subTest(seed=seed):
                grid, access, widths = random_board(seed)
                result = route_exact(grid, access, max_iters=4, via_cost=12.0)
                gap = copper_gaps(grid, result, widths)
                self.assertGreaterEqual(gap, grid.clearance - 1e-9)

    def test_recover_keeps_complete_routes_and_never_loses_connections(self):
        improved = 0
        for seed in range(25):
            grid, access, _ = random_board(seed)
            kwargs = dict(max_iters=4, via_cost=12.0, net_halo={n: 1 for n in access})
            kwargs["via_keepout"] = 3
            with patch.dict(os.environ, {"PNR_EXACT_SEPARATION": "off"}):
                plain = route(grid, access, **kwargs)
            with patch.dict(os.environ, {"PNR_EXACT_SEPARATION": "recover"}):
                recovered = route(grid, access, **kwargs)
            with self.subTest(seed=seed):
                if not plain.unrouted:
                    self.assertEqual(recovered, plain)
                self.assertLessEqual(missing_connections(recovered), missing_connections(plain))
                improved += missing_connections(recovered) < missing_connections(plain)
        self.assertGreater(improved, 0)

    def test_native_and_packed_exact_routes_match(self):
        kernel = native_kernel(self)
        for seed in range(10):
            grid, access, _ = random_board(seed)
            with patch.dict(os.environ, {"PNR_MAZE_KERNEL": "packed"}):
                packed = route_exact(grid, access, max_iters=4, via_cost=12.0)
            with patch.object(native_maze, "load", return_value=kernel), patch.dict(
                os.environ, {"PNR_MAZE_KERNEL": "native"}
            ):
                native = route_exact(grid, access, max_iters=4, via_cost=12.0)
            self.assertEqual(native, packed)

    def test_reference_kernel_keeps_the_halo_model(self):
        grid, access, _ = random_board(3)
        with patch.dict(os.environ, {"PNR_PACKED_MAZE": "0", "PNR_EXACT_SEPARATION": "full"}):
            reference = route(grid, access, max_iters=2)
        with patch.dict(os.environ, {"PNR_PACKED_MAZE": "0", "PNR_EXACT_SEPARATION": "off"}):
            self.assertEqual(route(grid, access, max_iters=2), reference)


def class_gaps(grid, result, widths):
    """Least (copper gap - the pair's clearance) between different nets (mm)."""
    classes = grid.net_clearances
    items = []
    for net, rn in result.nets.items():
        r = widths.get(net, grid.track_width) / 2
        c = max(grid.clearance, classes.get(net, grid.clearance))
        for layer, a, b in rn.segments:
            items.append((net, layer, grid.center_of(*a), grid.center_of(*b), r, c))
        for i, j in rn.vias:
            p = grid.center_of(i, j)
            for layer in range(grid.nlayers):
                items.append((net, layer, p, p, grid.via_radius, c))
    worst = math.inf
    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            a, b = items[x], items[y]
            if a[0] == b[0] or a[1] != b[1]:
                continue
            gap = math.sqrt(_segment_distance_sq(a[2], a[3], b[2], b[3])) - a[4] - b[4]
            worst = min(worst, gap - max(a[5], b[5]))
    return worst


class ClassClearanceTest(unittest.TestCase):
    """A net class's clearance above the fab clearance holds between two nets
    (the larger of the two, as KiCad judges it)."""

    def test_separation_table(self):
        grid = board(widths={"W": 0.6})
        grid.net_clearances = {"H": 0.5}
        sep = Separation(grid, ["S", "W", "H"])
        s, w, h = sep.kind["S"], sep.kind["W"], sep.kind["H"]
        self.assertAlmostEqual(sep.distance[s][s], 0.45)
        self.assertAlmostEqual(sep.distance[h][s], 0.75)
        self.assertAlmostEqual(sep.distance[h][w], 0.25 / 2 + 0.3 + 0.5)
        self.assertEqual(sep.via_kind["S"], sep.via)
        self.assertNotEqual(sep.via_kind["H"], sep.via)
        self.assertAlmostEqual(sep.distance[sep.via_kind["H"]][s], 0.3 + 0.125 + 0.5)
        self.assertAlmostEqual(sep.distance[sep.via_kind["H"]][sep.via], 1.1)
        # Without class clearances: one via kind, the plain table.
        plain = Separation(board(widths={"W": 0.6}), ["S", "W"])
        self.assertEqual(len(plain), 3)
        self.assertEqual(set(plain.via_kind.values()), {plain.via})

    def test_routes_keep_each_pair_of_classes_apart(self):
        with patch.dict(os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1"}):
            for seed in range(12):
                grid, access, widths = random_board(seed)
                rng = random.Random(100 + seed)
                grid.net_clearances = {
                    n: rng.choice((0.3, 0.5)) for n in sorted(access) if rng.random() < 0.4
                }
                result = route_exact(grid, access, max_iters=4, via_cost=12.0)
                with self.subTest(seed=seed):
                    self.assertGreaterEqual(class_gaps(grid, result, widths), -1e-9)


class DiagonalMaskParityTest(unittest.TestCase):
    """The 45° step block mask, packed against native."""

    def test_random_fields(self):
        from pnr.route.detail.dense_maze import GridStatic, build_exact_field
        from pnr.route.detail.packed_maze import _search, endpoints

        kernel = native_kernel(self)
        for seed in range(150):
            rng = random.Random(seed)
            grid = board(nx=rng.randrange(8, 16), ny=rng.randrange(8, 14))
            shape = (grid.nlayers, grid.ny, grid.nx)
            static = GridStatic(grid)
            field = build_exact_field(
                static,
                "N",
                track_count=np.array(
                    [rng.randrange(3) for _ in range(np.prod(shape))], dtype=np.int16
                ).reshape(shape),
                via_count=np.zeros(shape, dtype=np.int16),
                history=np.array([rng.random() for _ in range(np.prod(shape))]).reshape(shape),
                pres_fac=0.7,
                track_block=np.array([rng.random() < 0.15 for _ in range(np.prod(shape))]).reshape(
                    shape
                ),
                diag_block=np.array([rng.random() < 0.3 for _ in range(np.prod(shape))]).reshape(
                    shape
                ),
            )
            cells = [
                Cell(la, i, j)
                for la in range(grid.nlayers)
                for i in range(grid.nx)
                for j in range(grid.ny)
            ]
            found = endpoints(field, set(rng.sample(cells, 2)), set(rng.sample(cells, 2)))
            if found is None:
                continue
            starts, ends, target_xy = found
            expected = _search(field, starts, ends, target_xy, 12.0, True, ())
            self.assertEqual(kernel.search(field, starts, ends, 12.0, True, ()), expected)


if __name__ == "__main__":
    unittest.main()
