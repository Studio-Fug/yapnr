"""PNR_EXACT_LATE_ROOM: the exact-separation recovery beside late plane drops.

A plane net's surface pad without a planned drop gets its via from writeback after
routing, so the recovery (which packs nets as tightly as the rules allow) was skipped
on any board with one: on 12-soc-bga-113 that left 28 connections open which the
recovery closes to 2. With the flag the recovery runs and its route is kept only when
it leaves fewer connections open and every late drop the negotiated route left room
for keeps its room (:func:`pnr.route.detail.maze.late_drop_room`)."""

import os
import unittest
from unittest import mock

from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail import maze
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.maze import RoutedNet, RouteResult, late_drop_room

SITE = ("U1.9", "GND", (5.0, 3.0), 0, 0.6, 2.1)  # pad, net, centre, layer, via ring


def grid():
    return RouteGrid(10, 6, 0.25, layers=("F.Cu", "B.Cu"), via_radius=0.2)


def walled(g, layers=(0, 1), r0=0.4, r1=2.4):
    """A net S whose copper fills the ring around the site on ``layers``."""
    cells = []
    for la in layers:
        for j in range(g.ny):
            for i in range(g.nx):
                x, y = g.center_of(i, j)
                if r0 <= ((x - 5.0) ** 2 + (y - 3.0) ** 2) ** 0.5 <= r1:
                    cells.append(Cell(la, i, j))
    return RouteResult(
        nets={"S": RoutedNet("S", cells=cells, routed=True)}, unrouted=[], iterations=1
    )


class RoomTest(unittest.TestCase):
    def test_free_board_has_room(self):
        empty = RouteResult(nets={}, unrouted=[], iterations=0)
        self.assertEqual(late_drop_room(grid(), empty, [SITE], 1), frozenset({"U1.9"}))

    def test_foreign_copper_round_the_pad_takes_it(self):
        g = grid()
        self.assertEqual(late_drop_room(g, walled(g), [SITE], 1), frozenset())

    def test_own_copper_leaves_it(self):
        g = grid()
        own = walled(g)
        own.nets = {"GND": RoutedNet("GND", cells=own.nets["S"].cells, routed=True)}
        self.assertEqual(late_drop_room(g, own, [SITE], 1), frozenset({"U1.9"}))

    def test_a_via_column_needs_every_layer(self):
        g = grid()
        # Copper on B.Cu only: no via column, though the surface stub is free.
        self.assertEqual(late_drop_room(g, walled(g, layers=(1,)), [SITE], 1), frozenset())


class RecoveryTest(unittest.TestCase):
    """maze.route's choice, with the two routes stubbed."""

    def run_route(self, exact_result, flag=True):
        g = grid()
        halo = RouteResult(
            nets={
                "A": RoutedNet("A", routed=False, remaining_connections=3),
                "S": RoutedNet("S", routed=True),
            },
            unrouted=["A"],
            iterations=1,
        )
        sites = [SITE] if flag else None
        with mock.patch.object(maze, "_route_impl", return_value=halo), mock.patch(
            "pnr.route.detail.exact_route.supported", return_value=True
        ), mock.patch(
            "pnr.route.detail.exact_route.route_exact",
            **(
                {"side_effect": exact_result}
                if callable(exact_result)
                else {"return_value": exact_result}
            ),
        ) as exact, mock.patch.dict(
            os.environ, {"PNR_SINGLE_TRACK_WORKERS": "1", "PNR_EXACT_SEPARATION": "recover"}
        ), mock.patch(
            "pnr.runtime_controls.route_workers", return_value=1
        ):
            got = maze.route(
                g, {"A": []}, late_copper="1 plane-net surface pad (U1.9)", late_drops=sites
            )
        return got, exact

    def test_skipped_without_the_sites(self):
        got, exact = self.run_route(RouteResult(nets={}, unrouted=[], iterations=1), flag=False)
        self.assertEqual(got.unrouted, ["A"])
        exact.assert_not_called()

    def test_kept_when_the_drop_keeps_its_room(self):
        better = RouteResult(nets={"A": RoutedNet("A", routed=True)}, unrouted=[], iterations=2)
        got, exact = self.run_route(better)
        exact.assert_called_once()
        self.assertIs(got, better)

    def test_discarded_when_it_takes_the_drops_room(self):
        g = grid()
        greedy = walled(g)
        greedy.nets["A"] = RoutedNet("A", routed=True)
        got, _exact = self.run_route(greedy)
        self.assertEqual(got.unrouted, ["A"])

    def test_a_lost_drop_is_held_and_the_recovery_runs_again(self):
        # The first recovery walls the drop in; the drop's site (as the negotiated
        # route left it) is then held for its net and the second recovery keeps it.
        g = grid()
        greedy = walled(g)
        greedy.nets["A"] = RoutedNet("A", routed=True)
        roomy = RouteResult(nets={"A": RoutedNet("A", routed=True)}, unrouted=[], iterations=2)
        held = []

        def exact(grid_, *_args, **_kwargs):
            held.append([allowed for _t, _v, allowed in grid_.net_keepouts])
            return greedy if len(held) == 1 else roomy

        got, calls = self.run_route(exact)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(held, [[], [frozenset({"GND"})]])
        self.assertIs(got, roomy)

    def test_the_held_site_is_released_and_a_worse_retry_discarded(self):
        g = grid()
        greedy = walled(g)
        greedy.nets["A"] = RoutedNet("A", routed=True)
        got, calls = self.run_route(lambda *a, **k: greedy)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(got.unrouted, ["A"])


class ReserveTest(unittest.TestCase):
    def test_the_held_site_keeps_its_room(self):
        g = grid()
        empty = RouteResult(nets={}, unrouted=[], iterations=0)
        where = {}
        self.assertEqual(late_drop_room(g, empty, [SITE], 1, where=where), {"U1.9"})
        ((net, layer, centre, (i, j)),) = where.values()
        self.assertEqual((net, layer, centre), ("GND", 0, (5.0, 3.0)))
        access = {"S": [Cell(0, i + 1, j)]}
        track, via, allowed = maze._drop_reserve(g, where, access, 1)
        self.assertEqual(allowed, {"GND"})
        # The column (every layer) and the stub are held, the access cell is not.
        self.assertTrue(track[:, j, i].all() and via[:, j, i].all())
        self.assertFalse(track[0, j, i + 1])
        stub = g.cell_of(5.0, 3.0)
        self.assertTrue(track[0, stub[1], stub[0]])
        # Copper of another net anywhere outside the held cells leaves the room.
        cells = [
            Cell(la, a, b)
            for la in range(g.nlayers)
            for b in range(g.ny)
            for a in range(g.nx)
            if not track[la, b, a]
        ]
        other = RouteResult(
            nets={"S": RoutedNet("S", cells=cells, routed=True)}, unrouted=[], iterations=1
        )
        self.assertEqual(late_drop_room(g, other, [SITE], 1), {"U1.9"})
        self.assertIsNone(maze._drop_reserve(g, {}, access, 1))


class RouterSitesTest(unittest.TestCase):
    def test_sites_only_with_the_flag_and_without_deferred_nets(self):
        from pnr.route.detail.router import _late_drops, _late_pads

        part = Component(
            "C1",
            "c",
            (5.0, 3.0),
            0.0,
            "top",
            (1.6, 0.8),
            (1.6, 0.8),
            pads=[Pad("1", "GND", (-0.5, 0.0), (0.6, 0.6)), Pad("2", "S", (0.5, 0.0), (0.6, 0.6))],
        )
        graph = BoardGraph("t", [part], [Net("GND", 1, [("C1", "1")])])
        ((pad, net, xy, side, half),) = _late_pads(graph, {"GND"})
        self.assertEqual((pad, net, side), ("C1.1", "GND", "top"))
        self.assertAlmostEqual(xy[0], 4.5)
        fab = dict(via_diameter_mm=0.4, clearance_mm=0.1)
        with mock.patch.dict(os.environ, {"PNR_EXACT_LATE_ROOM": ""}):
            self.assertIsNone(_late_drops(grid(), graph, {"GND"}, set(), (), fab))
        with mock.patch.dict(os.environ, {"PNR_EXACT_LATE_ROOM": "1"}):
            self.assertIsNone(_late_drops(grid(), graph, {"GND"}, {"VBUS"}, (), fab))
            ((_, _, _, layer, near, far),) = _late_drops(grid(), graph, {"GND"}, set(), (), fab)
        self.assertEqual(layer, 0)
        self.assertAlmostEqual(near, half + 0.3)
        self.assertAlmostEqual(far, half + 1.8)

    def test_span_shares_writebacks_search_ring(self):
        """_late_drops' (near, far) is pnr.writeback.via_drop_span_mm exactly, so the
        two searches (this one on the signal grid, writeback's dog-bone fanout after
        routing) cannot drift apart (review finding on #85: maze.py:1173,
        router.py:129 used to re-derive the ring by hand)."""
        from pnr.route.detail.router import _late_drops, _late_pads
        from pnr.writeback import VIA_DROP_SEARCH_MM, via_drop_span_mm

        part = Component(
            "C1",
            "c",
            (5.0, 3.0),
            0.0,
            "top",
            (1.6, 0.8),
            (1.6, 0.8),
            pads=[Pad("1", "GND", (-0.5, 0.0), (0.6, 0.6))],
        )
        graph = BoardGraph("t", [part], [Net("GND", 1, [("C1", "1")])])
        ((_, _, _, _, half),) = _late_pads(graph, {"GND"})
        fab = dict(via_diameter_mm=0.4, clearance_mm=0.1)
        with mock.patch.dict(os.environ, {"PNR_EXACT_LATE_ROOM": "1"}):
            ((_, _, _, _, near, far),) = _late_drops(grid(), graph, {"GND"}, set(), (), fab)
        want_near, want_far = via_drop_span_mm(
            half, fab["via_diameter_mm"] / 2, fab["clearance_mm"]
        )
        self.assertAlmostEqual(near, want_near)
        self.assertAlmostEqual(far, want_far)
        self.assertAlmostEqual(far - near, VIA_DROP_SEARCH_MM[-1])


class ViaDropSpanTest(unittest.TestCase):
    def test_near_is_half_diagonal_plus_via_radius_plus_clearance(self):
        from pnr.writeback import via_drop_span_mm

        near, far = via_drop_span_mm(0.5, 0.2, 0.1)
        self.assertAlmostEqual(near, 0.8)
        self.assertAlmostEqual(far, 2.3)

    def test_far_is_near_plus_the_farthest_search_step(self):
        from pnr.writeback import VIA_DROP_SEARCH_MM, via_drop_span_mm

        near, far = via_drop_span_mm(1.1, 0.3, 0.2)
        self.assertAlmostEqual(far - near, VIA_DROP_SEARCH_MM[-1])


if __name__ == "__main__":
    unittest.main()
