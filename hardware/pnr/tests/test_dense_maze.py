"""Dense search fields and the packed kernel against the reference A*."""

import os
import random
import unittest
from collections import defaultdict
from unittest.mock import patch

import numpy as np

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
    args = dict(args)
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
        self.check({"PNR_PACKED_MAZE": "1"})


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
            halo = {n: rng.randrange(2) for n in names}
            yield grid, access, dict(
                max_iters=rng.randrange(1, 5),
                rrr_rounds=rng.randrange(1, 4),
                via_keepout=rng.randrange(1, 3),
                net_halo=halo,
                via_cost=rng.choice((3.0, 6.0)),
            )

    def routes(self, environ):
        out = []
        with patch.dict(os.environ, dict(environ, PNR_SINGLE_TRACK_WORKERS="1")):
            for grid, access, kwargs in self.boards():
                out.append(route(grid, access, **kwargs))
        return out

    def test_packed_routes_equal_reference(self):
        expected = self.routes({"PNR_PACKED_MAZE": "0"})
        self.assertTrue(any(r.unrouted for r in expected))  # the rip-up passes run
        self.assertEqual(self.routes({"PNR_PACKED_MAZE": "1"}), expected)


if __name__ == "__main__":
    unittest.main()
