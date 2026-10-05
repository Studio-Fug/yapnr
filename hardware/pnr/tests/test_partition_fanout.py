"""Plane partitions and declared fanouts together (stage 3c E4).

``plane_partition[*].protect_fanouts``: a declared fanout's planned access cells stay
free of the other nets' exits and plane drops (a drop via's keep-out closed a ball's
only way out on the ``-rails`` rung) and of the trunk cores' via keepouts. A fanout's
``bottom_sites`` part may drop a plane pad by a stub to the fanout via of its net
(the drop planner's ``reuse``) instead of drilling its own via under the array.
"""

import unittest

from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import pad_rects
from pnr.power_spec import PowerSpecError, parse_partition
from pnr.route.detail.grid import RouteGrid


def chip(ref, pos, a, b, side="top"):
    pads = [
        Pad("1", a, (-0.8, 0.0), (0.9, 0.95), land_corner=0.0),
        Pad("2", b, (0.8, 0.0), (0.9, 0.95), land_corner=0.0),
    ]
    return Component(ref, "0603", pos, 0.0, side, (3.0, 1.6), (3.0, 1.6), pads=pads)


def soic(ref, pos, nets):
    pads = []
    for k, net in enumerate(nets):
        x = -2.475 if k < 4 else 2.475
        y = 1.905 - 1.27 * (k if k < 4 else 7 - k)
        pads.append(Pad(str(k + 1), net, (x, y), (1.95, 0.6), land_corner=0.0))
    return Component(ref, "SO-8", pos, 0.0, "top", (7.0, 5.5), (7.0, 5.5), pads=pads)


def board(components, size):
    nets = {}
    for c in components:
        for p in c.pads:
            if p.net:
                nets.setdefault(p.net, []).append((c.ref, p.name))
    return BoardGraph(
        name="t",
        components=components,
        nets=[Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        outline=BoardOutline(*size),
    )


def grid_of(g, size):
    return RouteGrid.from_graph(
        g, size[0], size[1], pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
    )


class SpecTest(unittest.TestCase):
    def test_protect_fanouts_only_when_declared(self):
        base = dict(layer="In2.Cu", nets=["A"])
        (plain,) = parse_partition([base])
        self.assertNotIn("protect_fanouts", plain)
        (off,) = parse_partition([dict(base, protect_fanouts=False)])
        self.assertEqual(off, plain)  # false: the rules stay byte-identical
        (on,) = parse_partition([dict(base, protect_fanouts=True)])
        self.assertIs(on["protect_fanouts"], True)
        with self.assertRaisesRegex(PowerSpecError, "protect_fanouts must be a boolean"):
            parse_partition([dict(base, protect_fanouts="yes")])


class ReuseDropTest(unittest.TestCase):
    """A plane pad within reach of a planned via of its net drops by a stub to it."""

    def setUp(self):
        self.g = board([chip("C1", (5.0, 4.0), "VCC", "GND", side="bottom")], (10.0, 8.0))
        self.grid = grid_of(self.g, (10.0, 8.0))
        self.rect = pad_rects(self.g.component("C1"))[1][2]
        self.via = (self.rect.cx + 1.0, self.rect.cy)  # 0.55 mm land edge to via centre
        self.grid.escape_vias.append(("GND", self.via))
        self.grid.escape_vias.append(("VCC", (self.rect.cx, self.rect.cy + 1.2)))

    def drops(self, **kw):
        from pnr.route.detail.joint_escape import enumerate_drops

        side = self.grid.nlayers - 1
        point = (self.rect.cx, self.rect.cy)
        return enumerate_drops(
            self.grid,
            "GND",
            point,
            side,
            rect=self.rect,
            width=0.3,
            via_keepout=3,
            reach=8,
            **kw,
        )

    def test_the_stub_to_the_planned_via_comes_first(self):
        plain = self.drops()
        self.assertTrue(plain)
        self.assertTrue(all(o.vias for o in plain))  # every option drills a via
        vias = [v for n, v in self.grid.escape_vias if n == "GND"]
        options = self.drops(reuse=(vias, 0.6))
        first = options[0]
        self.assertEqual(first.vias, ())  # nothing drilled
        self.assertEqual(first.escape.via_xy, None)
        (seg,) = first.segments
        self.assertEqual(seg[1], (self.rect.cx, self.rect.cy))
        self.assertEqual(seg[2], self.via)
        self.assertLess(first.cost, min(o.cost for o in plain))

    def test_a_via_beyond_reach_or_of_another_net_is_not_reused(self):
        options = self.drops(reuse=([v for n, v in self.grid.escape_vias if n == "GND"], 0.1))
        self.assertTrue(all(o.vias for o in options))
        # Only the caller's vias are offered (route_board passes the net's own).
        other = (self.rect.cx, self.rect.cy + 1.2)
        options = self.drops(reuse=([other], 0.1))
        self.assertTrue(all(o.vias for o in options))

    def test_bottom_site_parts_name_their_pads(self):
        from pnr.route.detail.router import _bottom_site_reuse

        rules = dict(
            fanouts=[dict(ref="U1", bottom_sites=dict(parts=["C1", "C9"], max_stub_mm=0.5))]
        )
        self.assertEqual(_bottom_site_reuse(self.g, rules), {("C1", "1"): 0.5, ("C1", "2"): 0.5})
        self.assertEqual(_bottom_site_reuse(self.g, dict(fanouts=[dict(ref="U1")])), {})
        self.assertEqual(_bottom_site_reuse(self.g, None), {})


class GuardTest(unittest.TestCase):
    """grid.guard_access: no other net's exit or drop occupies a guarded cell."""

    def plan(self, guard=None):
        from pnr.route.detail.escape import plan_escapes

        g = board(
            [soic("U1", (6.0, 6.0), ["GND", "A", "GND", "B", "VCC", "C", "VCC", "D"])],
            (12.0, 12.0),
        )
        grid = grid_of(g, (12.0, 12.0))
        grid.net_widths = {"GND": 0.4, "VCC": 0.4}
        if guard is not None:
            grid.guard_access = guard
        plan = plan_escapes(
            grid,
            g,
            {"A", "B", "C", "D"},
            via_keepout=3,
            drop_widths={"GND": 0.4, "VCC": 0.4},
        )
        return grid, plan

    def test_a_guarded_cell_moves_the_drop_that_would_close_it(self):
        grid, plan = self.plan()
        drop = [e for e in plan.escapes if e.net == "GND" and e.via_xy is not None][0]
        cell = (0, *grid.cell_of(*drop.via_xy))
        # Another net's planned access cell at that via site (a fanout ball's tail).
        grid2, plan2 = self.plan({cell: "Z"})
        moved = [e for e in plan2.escapes if e.pad_xy == drop.pad_xy][0]
        self.assertIsNotNone(moved.via_xy)
        self.assertNotEqual(moved.via_xy, drop.via_xy)
        self.assertEqual(plan2.drop_failures, plan.drop_failures)  # no new failure
        self.assertNotEqual(grid2.pad_net.get(cell), "GND")  # the cell is left alone

    def test_without_a_guard_the_plan_is_unchanged(self):
        _g1, p1 = self.plan()
        _g2, p2 = self.plan({})
        key = [(e.net, e.pad_xy, e.via_xy, tuple(e.segments or ())) for e in p1.escapes]
        self.assertEqual(
            key, [(e.net, e.pad_xy, e.via_xy, tuple(e.segments or ())) for e in p2.escapes]
        )


class CoreSpareTest(unittest.TestCase):
    def test_a_spare_site_keeps_its_via_room(self):
        from types import SimpleNamespace

        from pnr.plane_partition import _CACHE, Terminal, _core_keepouts, partition

        masks = []
        grid = SimpleNamespace(
            nx=120,
            ny=60,
            pitch=0.1,
            width=12.0,
            height=6.0,
            nlayers=2,
            add_net_keepout=lambda track, via, allowed: masks.append(via),
        )
        _CACHE.clear()
        entry = dict(
            layer="In2.Cu",
            nets=["A"],
            order="current",
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill=None,
            core_no_vias=True,
            terminal_reach_mm=0.8,
            currents={},
            budgets_mohm={},
            sources={},
            h_mm=0.1,
        )
        terms = {
            "A": [Terminal("A1", "pad", (2.0, 3.0), 0.8), Terminal("A2", "pad", (10.0, 3.0), 0.8)]
        }
        part = partition(entry, width=12.0, height=6.0, terminals=terms, blocked=[])
        _core_keepouts(grid, part, 0.35)
        _core_keepouts(grid, part, 0.35, spare_sites=[((6.0, 3.0), 0.5)])
        plain, spared = masks
        self.assertTrue(plain[0, 30, 60])  # (6.05, 3.05): on the trunk
        self.assertFalse(spared[0, 30, 60])
        self.assertTrue(spared[0, 30, 40])  # (4.05, 3.05): still kept
        self.assertGreater(int(plain.sum()), int(spared.sum()))


if __name__ == "__main__":
    unittest.main()
