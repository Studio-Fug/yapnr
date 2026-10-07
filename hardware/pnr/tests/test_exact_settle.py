"""PNR_EXACT_SETTLE: the longer exact negotiation and the settle transactions."""

import os
import random
import unittest
from unittest.mock import patch

from tests.test_exact_route import board, copper_gaps, line

from pnr.route.detail import exact_route
from pnr.route.detail.exact_route import (
    Occupancy,
    Separation,
    Zone,
    missing_connections,
    route_exact,
    settle_mode,
)
from pnr.route.detail.grid import Cell
from pnr.route.detail.maze import remaining_connections


def crowded(seed):
    """Two layers, 10 to 15 two-terminal nets on a 16-22 x 12-18 cell grid: dense
    enough that the exact route leaves connections open."""
    rng = random.Random(seed)
    names = ["N%d" % k for k in range(rng.randrange(10, 16))]
    grid = board(nx=rng.randrange(16, 22), ny=rng.randrange(12, 18))
    for _ in range(rng.randrange(0, 20)):
        grid.blocked[
            rng.randrange(grid.nlayers), rng.randrange(grid.ny), rng.randrange(grid.nx)
        ] = True
    access = {
        n: [
            Cell(
                rng.randrange(grid.nlayers),
                rng.randrange(1, grid.nx - 1),
                rng.randrange(1, grid.ny - 1),
            )
            for _ in range(2)
        ]
        for n in names
    }
    return grid, access


def branches(access, routes):
    return sum(
        max(0, len(set(access[n])) - 1 - remaining_connections(access[n], r.edges))
        for n, r in routes.items()
        if r is not None
    )


class SettleTest(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1"})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def route(self, seed, settle):
        grid, access = crowded(seed)
        with patch.dict(os.environ, {"PNR_EXACT_SETTLE": "1" if settle else "0"}):
            return grid, access, route_exact(grid, access, max_iters=4, via_cost=12.0)

    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(settle_mode())
        with patch.dict(os.environ, {"PNR_EXACT_SETTLE": "1"}):
            self.assertTrue(settle_mode())

    def test_transactions_only_add_branches_and_keep_the_exact_clearance(self):
        calls = []
        original = exact_route._settle

        def recorded(grid, sep, nets, access, snap, **kwargs):
            out = original(grid, sep, nets, access, snap, **kwargs)
            calls.append((grid, sep, access, dict(snap), out, list(exact_route._settle.kept)))
            return out

        kept = 0
        with patch.object(exact_route, "_settle", recorded):
            for seed in range(12):
                grid, access, result = self.route(seed, True)
                with self.subTest(seed=seed):
                    self.assertGreaterEqual(copper_gaps(grid, result, {}), grid.clearance - 1e-9)
        for grid, sep, access, before, after, transactions in calls:
            # Never fewer connected branches; one more per transaction kept at least.
            self.assertGreaterEqual(
                branches(access, after), branches(access, before) + len(transactions)
            )
            # The settled state is conflict-free under the exact rule.
            occupancy = Occupancy(grid, sep)
            for net, route in after.items():
                zone = Zone(grid, sep, net, route)
                self.assertFalse(occupancy.conflicts(zone), net)
                occupancy.add(zone)
            kept += len(transactions)
        self.assertGreater(kept, 0)

    def test_settling_leaves_fewer_connections_open_overall(self):
        better = worse = 0
        for seed in range(20):
            _, _, plain = self.route(seed, False)
            _, _, settled = self.route(seed, True)
            better += missing_connections(settled) < missing_connections(plain)
            worse += missing_connections(settled) > missing_connections(plain)
        self.assertGreater(better, 2 * worse)

    def test_a_contested_channel_goes_to_the_net_that_leaves_both_connected(self):
        # A row 6 wall with a one-track gap at column 10 and an opening at its east end.
        # A's committed route takes the gap; B, whose way round is long, routes through A
        # at a price, and A routes again round the east end: both connected.
        grid = board(nx=60, ny=13, layers=("F.Cu",))
        for i in range(0, 60):
            if i != 10 and i < 56:
                grid.blocked[0, 6, i] = True
        access = {
            "A": [Cell(0, 10, 3), Cell(0, 10, 9)],
            "B": [Cell(0, 6, 1), Cell(0, 6, 11)],
        }
        for net in access:
            for c in access[net]:
                grid.blocked[c.layer, c.j, c.i] = False
        through = line(0, [(10, j) for j in range(3, 10)])
        sep = Separation(grid, sorted(access))
        from pnr.route.detail.dense_maze import DenseSession, build_exact_field
        from pnr.route.detail.maze import _route_one

        session = DenseSession(grid)

        def route_net(net, field):
            return _route_one(
                grid, access[net], net, {}, {}, 12.0, 0.0, _session=session, _field=field
            )

        def blocked_field(net, committed):
            track, via, step = committed.blocked(net)
            return build_exact_field(
                session.static, net, track_block=track, via_block=via, diag_block=step
            )

        def soft_field(net, committed, price):
            import numpy as np

            track, via, _ = committed.blocked(net)
            return build_exact_field(
                session.static,
                net,
                track_soft=np.where(track, price, 0.0),
                via_soft=np.where(via, price, 0.0),
            )

        out = exact_route._settle(
            grid,
            sep,
            sorted(access),
            access,
            {"A": through},
            route_net=route_net,
            blocked_field=blocked_field,
            soft_field=soft_field,
            order=lambda n: n,
            prices=(4.0, 16.0, 64.0),
        )
        self.assertEqual(sorted(out), ["A", "B"])
        for net in access:
            self.assertEqual(remaining_connections(access[net], out[net].edges), 0, net)
        self.assertEqual(exact_route._settle.kept[0]["net"], "B")
        self.assertEqual(exact_route._settle.kept[0]["displaced"], ["A"])
        self.assertTrue(any(c.i >= 56 for c in out["A"].cells))


if __name__ == "__main__":
    unittest.main()
