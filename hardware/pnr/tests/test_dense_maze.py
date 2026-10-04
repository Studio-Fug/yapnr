"""Dense search fields and the packed/native kernels against the reference A*."""

import os
import random
import shutil
import tempfile
import unittest
from collections import defaultdict
from unittest.mock import patch

import numpy as np

from pnr.route.detail import native_maze
from pnr.route.detail.dense_maze import (
    DenseCounts,
    DenseOwners,
    SoftOwners,
    build_field,
)
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.maze import _astar_reference, route
from pnr.route.detail.packed_maze import astar

LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def random_grid(rng, nx=None, ny=None, layers=None):
    """A grid with every predicate the kernels read: obstacles, via blocks, SMD
    via verdicts, pad and via-halo owners, plated ports, drills, escape vias."""
    nx = nx or rng.randrange(6, 16)
    ny = ny or rng.randrange(6, 14)
    layers = layers or LAYERS[: rng.choice((2, 2, 3, 4))]
    pitch = rng.choice((0.25, 0.3, 0.45, 1.0))
    grid = RouteGrid(nx * pitch, ny * pitch, pitch, layers=layers)
    nx, ny = grid.nx, grid.ny
    grid.routing_track_halos = {"N": rng.randrange(3), "M": rng.randrange(2)}
    grid.routing_via_keepout = rng.randrange(4)
    cells = [Cell(la, i, j) for la in range(grid.nlayers) for i in range(nx) for j in range(ny)]
    for c in rng.sample(cells, len(cells) // 10):
        grid.blocked[c.layer, c.j, c.i] = True
    for c in rng.sample(cells, len(cells) // 10):
        grid.via_blocked[c.layer, c.j, c.i] = True
    for c in rng.sample(cells, len(cells) // 12):
        grid.pad_net[c.layer, c.i, c.j] = rng.choice(("OTHER", "N", "", "\0conflict"))
    for c in rng.sample(cells, len(cells) // 12):
        grid.via_halo[c.layer, c.i, c.j] = rng.choice(("OTHER", "N", "M"))
    if rng.random() < 0.5:
        grid.smd_via_blocked = np.zeros((ny, nx), dtype=bool)
        for _ in range(nx * ny // 10):
            grid.smd_via_blocked[rng.randrange(ny), rng.randrange(nx)] = True

    def point():
        return (rng.uniform(0, nx * pitch), rng.uniform(0, ny * pitch))

    grid.source_drills = [(point(), rng.uniform(0.1, 0.5)) for _ in range(rng.randrange(3))]
    grid.source_drill_plated = [rng.choice((True, False, None)) for _ in grid.source_drills]
    if rng.random() < 0.5:
        grid.pth_hole_gap = rng.uniform(0.1, 0.4)
        grid.npth_hole_gap = rng.uniform(0.1, 0.4)
        grid.component_pth_min_drill = 0.3
        grid.via_hole_gap = rng.uniform(0.1, 0.3)
    grid.escape_vias = [(rng.choice(("N", "OTHER")), point()) for _ in range(rng.randrange(3))]
    grid.via_spacing = rng.uniform(0.3, 3.0) * pitch
    grid.via_drill_radius = 0.15
    centres = [grid.center_of(rng.randrange(nx), rng.randrange(ny)) for _ in range(2)]
    grid.plated_ports = [
        (rng.choice(("N", "OTHER")), c, rng.uniform(0.5, 2) * pitch) for c in centres
    ]
    grid.net_widths = {"N": rng.choice((0.1, 0.2))}
    if rng.random() < 0.5:
        # A wide net's own-width pad halos (RouteGrid.reserve_wide_pad_clearance).
        grid.wide_pad_net["N"] = {
            (c.layer, c.i, c.j): rng.choice(("OTHER", "N", "", "\0conflict"))
            for c in rng.sample(cells, len(cells) // 8)
        }
    return grid, cells


def random_search(rng, grid, cells):
    nx, ny = grid.nx, grid.ny
    outside = [Cell(rng.randrange(grid.nlayers), -1, rng.randrange(ny)), Cell(0, nx, 0)]
    blocked = set(rng.sample(cells, len(cells) // 15)) | set(outside[: rng.randrange(3)])
    return dict(
        sources=set(rng.sample(cells, rng.randrange(1, 4))),
        targets=set(rng.sample(cells, rng.randrange(1, 5))),
        net="N",
        occ={c: rng.randrange(4) for c in rng.sample(cells, len(cells) // 5)},
        history={c: rng.random() * 3 for c in rng.sample(cells, len(cells) // 6)},
        via_cost=rng.choice((3.0, 12.0, 0.5)),
        pres_fac=rng.choice((0.0, 0.5, 1.7)),
        blocked=blocked if rng.random() < 0.7 else None,
        soft=(
            {c: rng.random() * 4 for c in rng.sample(cells, len(cells) // 6)}
            if rng.random() < 0.6
            else None
        ),
        diagonal=rng.random() < 0.7,
        drill_sites=tuple(
            grid.center_of(rng.randrange(nx), rng.randrange(ny)) for _ in range(rng.randrange(3))
        ),
    )


def reference(grid, args):
    args = dict(args)
    return _astar_reference(
        grid,
        set(args.pop("sources")),
        set(args.pop("targets")),
        args.pop("net"),
        args.pop("occ"),
        args.pop("history"),
        args.pop("via_cost"),
        args.pop("pres_fac"),
        **args,
    )


def packed(grid, args):
    """The packed (or native) search on a dense field. The field must exist, so a
    parity test never compares the reference with itself; only a drill stencil on
    a threshold (``drill_stencil`` None) leaves the reference to search."""
    from pnr.route.detail.dense_maze import drill_stencil

    args = dict(args)
    field = build_field(
        grid,
        args["net"],
        args["occ"],
        args["history"],
        args["pres_fac"],
        args.get("blocked"),
        args.get("soft"),
    )
    assert field is not None or drill_stencil(grid) is None, "dense field missing"
    return astar(
        grid,
        set(args.pop("sources")),
        set(args.pop("targets")),
        args.pop("net"),
        args.pop("occ"),
        args.pop("history"),
        args.pop("via_cost"),
        args.pop("pres_fac"),
        **args,
        field=field,
    )


def cell_cost(grid, net, occ, history, soft, pres_fac, c, via):
    """The reference kernel's price, written out."""
    track = grid.routing_track_halos.get(net, 0)
    radius = max(track, grid.routing_via_keepout) if via else track
    layers = range(grid.nlayers) if via else (c.layer,)
    cells = [
        Cell(la, c.i + di, c.j + dj)
        for la in layers
        for di in range(-radius, radius + 1)
        for dj in range(-radius, radius + 1)
        if grid.in_bounds(c.i + di, c.j + dj)
    ]
    present = 1.0 + pres_fac * max([0] + [occ.get(p, 0) for p in cells])
    soft = soft or {}
    return (1.0 + max(history.get(p, 0.0) for p in cells)) * present + max(
        soft.get(p, 0.0) for p in cells
    )


def native_kernel(test):
    """Load the native kernel, compiling it with the host compiler when the
    library is not provided (Bazel provides it); skip without one."""
    native_maze.reset()
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
        test.skipTest("native maze library unavailable: " + native_maze._STATE["reason"])
    return kernel


class DenseFieldTest(unittest.TestCase):
    def test_prices_equal_reference_cell_cost(self):
        for seed in range(40):
            rng = random.Random(seed)
            grid, cells = random_grid(rng)
            args = random_search(rng, grid, cells)
            field = build_field(
                grid,
                "N",
                args["occ"],
                args["history"],
                args["pres_fac"],
                args["blocked"],
                args["soft"],
            )
            plane = grid.nx * grid.ny
            for c in rng.sample(cells, 30):
                key = c.layer * plane + c.j * grid.nx + c.i
                expected = cell_cost(
                    grid,
                    "N",
                    args["occ"],
                    args["history"],
                    args["soft"],
                    args["pres_fac"],
                    c,
                    False,
                )
                self.assertEqual(field.price[key], expected)
                expected = cell_cost(
                    grid, "N", args["occ"], args["history"], args["soft"], args["pres_fac"], c, True
                )
                self.assertEqual(field.via_price[c.j * grid.nx + c.i], expected)

    def test_predicates_equal_grid_queries(self):
        for seed in range(40):
            rng = random.Random(seed)
            grid, cells = random_grid(rng)
            args = random_search(rng, grid, cells)
            field = build_field(grid, "N", {}, {}, 0.0, args["blocked"])
            plane = grid.nx * grid.ny
            for c in cells:
                key = c.layer * plane + c.j * grid.nx + c.i
                self.assertEqual(
                    bool(field.plated[c.j * grid.nx + c.i]),
                    grid.plated_transition("N", c.i, c.j) is not None,
                )
                self.assertEqual(
                    bool(field.hole[c.j, c.i]), grid.hole_site_clear(grid.center_of(c.i, c.j))
                )
                if not args["blocked"]:
                    self.assertEqual(bool(field.ok[key]), grid.passable(c.layer, c.i, c.j, "N"))
            sites = args["drill_sites"]
            tree = field.tree_hole(sites)
            for c in cells:
                self.assertEqual(
                    bool(tree[c.j, c.i]), grid.hole_site_clear(grid.center_of(c.i, c.j), sites)
                )

    def test_incremental_tables_equal_recomputation(self):
        rng = random.Random(7)
        grid, cells = random_grid(rng, 12, 10)
        occ = DenseCounts(int, grid)
        history = DenseCounts(float, grid)
        owners = DenseOwners(grid)
        plain_occ, plain_history, plain_owners = defaultdict(int), defaultdict(float), {}
        for step in range(400):
            c = rng.choice(cells)
            action = rng.randrange(5)
            if action == 0:
                occ[c] += 1
                plain_occ[c] += 1
            elif action == 1 and plain_occ.get(c, 0) > 0:
                occ[c] -= 1
                plain_occ[c] -= 1
            elif action == 2:
                history[c] += rng.random()
                plain_history[c] += history[c] - plain_history[c]
            elif action == 3:
                net = rng.choice(("N", "M", "P"))
                owners[c] = net
                plain_owners[c] = net
            elif c in plain_owners:
                del owners[c]
                del plain_owners[c]
            # Reads never insert into the mirrors.
            occ.get(rng.choice(cells), 0)
        for mirror, plain in ((occ, plain_occ), (history, plain_history)):
            self.assertEqual(dict(mirror), dict(plain))
            expected = np.zeros_like(mirror.array)
            for c, value in plain.items():
                expected[c.layer, c.j, c.i] = value
            self.assertTrue(np.array_equal(mirror.array, expected))
        self.assertEqual(dict(owners), plain_owners)
        for net in ("N", "M", "Q"):
            soft = SoftOwners(owners, net, 6.0)
            expected = {c: 6.0 for c, o in plain_owners.items() if o != net}
            self.assertEqual(soft.as_dict(), expected)
            array = np.zeros((grid.nlayers, grid.ny, grid.nx))
            for c in expected:
                array[c.layer, c.j, c.i] = 6.0
            self.assertTrue(np.array_equal(soft.array(grid), array))

    def test_mirrored_inputs_price_like_dictionaries(self):
        rng = random.Random(3)
        grid, cells = random_grid(rng, 10, 9)
        occ, history, owners = DenseCounts(int, grid), DenseCounts(float, grid), DenseOwners(grid)
        for c in rng.sample(cells, 40):
            occ[c] += rng.randrange(1, 3)
            history[c] += rng.random()
            owners[c] = rng.choice(("N", "M"))
        soft = SoftOwners(owners, "N", 8.0)
        dense = build_field(grid, "N", occ, history, 1.1, None, soft)
        plain = build_field(grid, "N", dict(occ), dict(history), 1.1, None, soft.as_dict())
        self.assertTrue(np.array_equal(dense.price, plain.price))
        self.assertTrue(np.array_equal(dense.via_price, plain.via_price))


class KernelParityTest(unittest.TestCase):
    SEEDS = 400

    def check(self, kernel_env):
        for seed in range(self.SEEDS):
            rng = random.Random(seed)
            grid, cells = random_grid(rng)
            for _ in range(3):
                args = random_search(rng, grid, cells)
                with self.subTest(seed=seed):
                    expected = reference(grid, args)
                    with patch.dict(os.environ, kernel_env):
                        self.assertEqual(packed(grid, args), expected)

    def test_packed_matches_reference(self):
        self.check({"PNR_MAZE_KERNEL": "packed"})

    def test_native_matches_reference(self):
        kernel = native_kernel(self)
        with patch.object(native_maze, "load", return_value=kernel):
            self.check({"PNR_MAZE_KERNEL": "native"})

    def test_native_runs_when_selected(self):
        kernel = native_kernel(self)
        grid = RouteGrid(6, 6, 1)
        calls = []
        original = kernel.search

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with patch.object(native_maze, "load", return_value=kernel), patch.object(
            kernel, "search", side_effect=spy
        ), patch.dict(os.environ, {"PNR_MAZE_KERNEL": "native"}):
            path = astar(grid, {Cell(0, 0, 0)}, {Cell(1, 5, 5)}, "N", {}, {}, 3.0, 0.5)
        self.assertTrue(calls)
        self.assertEqual(path[0], Cell(0, 0, 0))
        self.assertEqual(path[-1], Cell(1, 5, 5))

    def test_missing_library_falls_back_to_packed(self):
        native_maze.reset()
        self.addCleanup(native_maze.reset)
        with patch.dict(
            os.environ, {"PNR_MAZE_KERNEL": "native", "PNR_MAZE_LIB": "/nonexistent/lib.so"}
        ), patch.object(native_maze, "_candidates", return_value=iter(())):
            self.assertIsNone(native_maze.active())
            self.assertEqual(native_maze.status()["kernel"], "packed")
            grid = RouteGrid(5, 5, 1)
            self.assertIsNotNone(
                astar(grid, {Cell(0, 0, 0)}, {Cell(0, 4, 4)}, "N", {}, {}, 3.0, 0.5)
            )


class RouteParityTest(unittest.TestCase):
    """Whole negotiated routes (negotiation, commit, rip-up, recovery)."""

    def boards(self):
        for seed in range(12):
            rng = random.Random(100 + seed)
            grid = RouteGrid(14, 12, 1, layers=LAYERS[: rng.choice((2, 4))])
            for _ in range(10):
                grid.blocked[
                    rng.randrange(grid.nlayers), rng.randrange(grid.ny), rng.randrange(grid.nx)
                ] = True
            grid.source_drills = [((rng.uniform(0, 14), rng.uniform(0, 12)), 0.4)]
            grid.via_spacing = 1.5
            names = ["A", "B", "C", "D", "E", "F"]
            access = {
                n: [
                    Cell(rng.randrange(grid.nlayers), rng.randrange(14), rng.randrange(12))
                    for _ in range(rng.randrange(2, 5))
                ]
                for n in names
            }
            if seed % 2:
                # Net A is a wide net with its own-width pad halos.
                grid.wide_pad_net["A"] = {
                    (rng.randrange(grid.nlayers), rng.randrange(14), rng.randrange(12)): (
                        rng.choice(("B", "A", ""))
                    )
                    for _ in range(20)
                }
            halo = {n: rng.randrange(2) for n in names}
            yield grid, access, dict(
                max_iters=rng.randrange(1, 5),
                rrr_rounds=rng.randrange(1, 4),
                via_keepout=rng.randrange(1, 3),
                net_halo=halo,
                via_cost=rng.choice((3.0, 6.0)),
            )

    def routes(self, environ, workers="1"):
        # The halo model on every kernel (the exact-separation recovery needs a dense
        # kernel, so the reference kernel never runs it; test_exact_route covers it).
        from pnr.route.detail.dense_maze import DenseSession

        out = []
        with patch.dict(
            os.environ,
            dict(environ, PNR_SINGLE_TRACK_WORKERS=workers, PNR_EXACT_SEPARATION="off"),
        ):
            for grid, access, kwargs in self.boards():
                self.assertIsNotNone(DenseSession(grid).static)  # the dense path runs
                out.append(route(grid, access, **kwargs))
        return out

    def test_packed_and_native_routes_equal_reference(self):
        expected = self.routes({"PNR_PACKED_MAZE": "0"})
        self.assertTrue(any(r.unrouted for r in expected))  # the rip-up passes run
        self.assertEqual(self.routes({"PNR_MAZE_KERNEL": "packed"}), expected)
        kernel = native_kernel(self)
        with patch.object(native_maze, "load", return_value=kernel):
            self.assertEqual(self.routes({"PNR_MAZE_KERNEL": "native"}), expected)

    def test_parallel_workers_route_like_the_reference(self):
        # Two route workers: the grid and the occupancy snapshots cross a process
        # boundary (the mirrored tables arrive as plain dictionaries).
        expected = self.routes({"PNR_PACKED_MAZE": "0"}, workers="2")
        self.assertEqual(self.routes({"PNR_MAZE_KERNEL": "packed"}, workers="2"), expected)


def fixture_grid(wide_net=None):
    """The detailed grid ``route_board`` builds for the splanc_dev fixture (its
    rules, planes and escape plan), captured where routing would start."""
    import yaml

    from pnr.constraints import compile_constraints, compile_routing_rules
    from pnr.graph import BoardGraph
    from pnr.route.detail import router
    from pnr.route.detail.maze import RouteResult

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testdata", "splanc_dev")
    with open(os.path.join(here, "graph.json"), encoding="utf-8") as fh:
        graph = BoardGraph.from_json(fh.read())
    with open(os.path.join(here, "constraints.yaml"), encoding="utf-8") as fh:
        constraints = compile_constraints(yaml.safe_load(fh), graph.refs)
    rules = compile_routing_rules(constraints, [n.name for n in graph.nets])
    if wide_net:
        rules["net_classes"].append(dict(name="wide", width_mm=0.9, nets=[wide_net]))
    captured = {}

    def capture(grid, net_access, **kwargs):
        grid.routing_track_halos = kwargs.get("net_halo") or {}
        grid.routing_via_keepout = kwargs.get("via_keepout", 1)
        grid.reserve_wide_pad_clearance()
        captured.update(grid=grid, access=net_access)
        return RouteResult(nets={}, unrouted=[], iterations=0)

    with patch.object(router, "route", capture):
        router.route_board(graph, constraints, rules, pitch=0.5, max_iters=2)
    return captured["grid"], captured["access"]


class GridModelGuardTest(unittest.TestCase):
    """The dense fields stand in for the grid predicates and the reference search
    only while those are what the fields were checked against."""

    def test_modelled_sources_are_current(self):
        from pnr.route.detail.dense_maze import MODELLED_SOURCES, model_sources

        self.assertEqual(
            model_sources(),
            MODELLED_SOURCES,
            "RouteGrid's predicates or the reference A* changed: port the change to the "
            "dense fields (dense_maze), the packed and native kernels and the exact "
            "router, check parity (this file, test_exact_route), then update "
            "dense_maze.MODELLED_SOURCES (and MODELLED_ATTRIBUTES for new grid state).",
        )

    def test_real_board_grid_matches_the_fields(self):
        from pnr.route.detail.dense_maze import GridStatic, unmodelled

        grid, access = fixture_grid(wide_net="SCL")
        self.assertIsNone(unmodelled(grid))
        self.assertIn("SCL", grid.wide_pad_net)
        static = GridStatic(grid)
        hole = static.hole()
        nets = sorted(access)[:12] + ["SCL", "lv"]
        for net in nets:
            passable, via, plated = static.net(net)
            for la in range(grid.nlayers):
                for j in range(grid.ny):
                    for i in range(grid.nx):
                        self.assertEqual(
                            bool(passable[la, j, i]), grid.passable(la, i, j, net), (net, la, i, j)
                        )
                        self.assertEqual(
                            bool(via[la, j, i]), grid.via_passable(la, i, j, net), (net, la, i, j)
                        )
            for j in range(grid.ny):
                for i in range(grid.nx):
                    self.assertEqual(
                        bool(plated[j, i]), grid.plated_transition(net, i, j) is not None
                    )
        for j in range(grid.ny):
            for i in range(grid.nx):
                self.assertEqual(bool(hole[j, i]), grid.hole_site_clear(grid.center_of(i, j)))

    def test_unmodelled_grid_state_routes_with_the_reference(self):
        import io
        from contextlib import redirect_stderr

        from pnr.route.detail import dense_maze

        grid = RouteGrid(8, 8, 1)
        access = {"A": [Cell(0, 0, 3), Cell(0, 7, 3)], "B": [Cell(0, 3, 0), Cell(1, 3, 7)]}
        # Grid state the fields do not model (as a stack's layer mask was before
        # the fields learnt it): any attribute outside MODELLED_ATTRIBUTES.
        grid.unmodelled_feature = None  # neutral: modelled as absent
        self.assertTrue(dense_maze.supports(grid))
        grid.unmodelled_feature = {"A": frozenset({1})}
        log = io.StringIO()
        with redirect_stderr(log):
            self.assertFalse(dense_maze.supports(grid))
            self.assertIsNone(dense_maze.DenseSession(grid).static)
        self.assertIn("unmodelled_feature", log.getvalue())
        with patch.dict(os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1"}):
            with patch.dict(os.environ, {"PNR_PACKED_MAZE": "0"}):
                expected = route(grid, access, max_iters=2)
            self.assertEqual(route(grid, access, max_iters=2), expected)
        with patch.object(dense_maze, "_STALE", ["RouteGrid.passable"]):
            self.assertIn("RouteGrid.passable", dense_maze.unmodelled(RouteGrid(4, 4, 1)))

    def test_status_names_a_reference_fallback(self):
        # A run whose grid the fields do not model (a via model) records that its
        # searches ran on the reference kernel, not only the kernel it selected.
        import io
        from contextlib import redirect_stderr

        from pnr.route.detail import dense_maze

        with patch.object(dense_maze, "_WARNED", set()), patch.dict(os.environ):
            os.environ.pop("PNR_PACKED_MAZE", None)
            os.environ.pop("PNR_MAZE_KERNEL", None)
            self.assertEqual(native_maze.status(), dict(kernel="packed", reason=""))
            grid = RouteGrid(4, 4, 1)
            grid.via_model = {"spans": [(0, 1)]}
            with redirect_stderr(io.StringIO()):
                self.assertFalse(dense_maze.supports(grid))
            status = native_maze.status()
            self.assertEqual(status["kernel"], "packed")
            self.assertEqual(len(status["reference_fallback"]), 1)
            self.assertIn("via_model", status["reference_fallback"][0])
            os.environ["PNR_PACKED_MAZE"] = "0"
            self.assertNotIn("reference_fallback", native_maze.status())

    def test_one_static_table_per_tree_without_a_session(self):
        from pnr.route.detail import dense_maze
        from pnr.route.detail.maze import _route_one

        built = []
        original = dense_maze.GridStatic.__init__

        def counting(self, grid):
            built.append(1)
            original(self, grid)

        grid = RouteGrid(10, 10, 1)
        access = [Cell(0, 0, 0), Cell(0, 9, 0), Cell(1, 9, 9), Cell(0, 0, 9)]
        with patch.object(dense_maze.GridStatic, "__init__", counting):
            tree = _route_one(grid, access, "N", {}, {}, 3.0, 0.5)
        self.assertEqual(len(built), 1)
        with patch.dict(os.environ, {"PNR_PACKED_MAZE": "0"}):
            self.assertEqual(_route_one(grid, access, "N", {}, {}, 3.0, 0.5), tree)

    def test_drill_stencil_on_a_threshold_builds_no_tables(self):
        from pnr.route.detail import dense_maze
        from pnr.route.detail.maze import _route_one

        grid = RouteGrid(10, 10, 1)
        grid.via_spacing = 2.0 + 1e-7  # an offset of exactly 2 cells sits on the rule
        self.assertIsNone(dense_maze.drill_stencil(grid))
        built = []
        original = dense_maze.GridStatic.__init__

        def counting(self, grid):
            built.append(1)
            original(self, grid)

        access = [Cell(0, 0, 0), Cell(1, 9, 9), Cell(0, 0, 9)]
        with patch.object(dense_maze.GridStatic, "__init__", counting):
            tree = _route_one(grid, access, "N", {}, {}, 3.0, 0.5)
        self.assertEqual(built, [])
        with patch.dict(os.environ, {"PNR_PACKED_MAZE": "0"}):
            self.assertEqual(_route_one(grid, access, "N", {}, {}, 3.0, 0.5), tree)


if __name__ == "__main__":
    unittest.main()
