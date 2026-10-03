"""Blind, buried and micro vias in the detailed router (pnr.via_policy on the grid).

A via under a via model occupies, is checked on, reserved on and priced for only
its own span's grid layers; through vias keep the legacy model exactly.
"""

import math
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_stack_route import compiled, sample, typed  # noqa: E402

from pnr.route.detail.grid import Cell, RouteGrid  # noqa: E402
from pnr.route.detail.maze import _astar, _footprint, route  # noqa: E402
from pnr.route.detail.router import route_board  # noqa: E402
from pnr.stack import copper_names  # noqa: E402
from pnr.via_policy import BLIND, BURIED, MICRO, THROUGH, GridVias, resolve  # noqa: E402

SIX = copper_names(6)
GAPS6 = [0.09, 0.55, 0.2, 0.55, 0.09]
HDI = dict(allowed=[THROUGH, BLIND, BURIED, MICRO], microvia=dict(diameter_mm=0.3, drill_mm=0.1))
BB = dict(allowed=[THROUGH, BLIND, BURIED])
GRID_LAYERS = ("F.Cu", "In2.Cu", "B.Cu")  # the routed layers of SGSGPS


def grid(n=12, policy=BB, keepout=1, pitch=1.0):
    g = RouteGrid(n, n, pitch, layers=GRID_LAYERS, clearance=0.2, via_radius=0.3)
    g.via_spacing = 0.55
    if policy is not None:
        p = resolve(policy, SIX, gaps=GAPS6, banned=() if MICRO in policy["allowed"] else {MICRO})
        g.via_model = GridVias(p, GRID_LAYERS, pitch, 0.2, (0.6, 0.3), keepout)
    return g


class GridSpans(unittest.TestCase):
    def test_blind_via_where_through_is_blocked(self):
        """F to In2 at a column whose B cell is foreign: a blind via passes, a
        through via cannot."""
        args = ({Cell(0, 0, 0)}, {Cell(1, 0, 0)}, "N", {}, {}, 5, 1)
        for policy, ok in ((None, False), (BB, True)):
            g = grid(1, policy)
            g.pad_net[(2, 0, 0)] = "OTHER"
            path = _astar(g, *args)
            self.assertEqual(path is not None, ok, policy)

    def test_through_span_still_checks_every_layer(self):
        g = grid(1, BB)
        g.pad_net[(1, 0, 0)] = "OTHER"
        self.assertIsNone(_astar(g, {Cell(0, 0, 0)}, {Cell(2, 0, 0)}, "N", {}, {}, 5, 1))

    def test_via_priced_by_its_span(self):
        """A layer change costs the via cost times its span's multiplier: an F-In2
        blind via (0.72) is cheaper than a through via (1.0), and F to B takes the
        direct through via rather than two blind ones (0.72 + 0.78)."""
        from pnr.route.detail.maze import _astar as astar

        g = grid(1, BB)
        fi = g.via_model.span(0, 1)
        self.assertLess(fi.cost, g.via_model.span(0, 2).cost)
        path = astar(g, {Cell(0, 0, 0)}, {Cell(2, 0, 0)}, "N", {}, {}, 10.0, 1)
        self.assertEqual([c.layer for c in path], [0, 2])

    def test_footprint_only_on_span_layers(self):
        g = grid(12, BB, keepout=1)
        cells = [Cell(0, 5, 5), Cell(1, 5, 5)]
        fp = _footprint(g, cells, 1, edges=[(cells[0], cells[1])], net="N")
        self.assertTrue(any(c.layer == 1 for c in fp))
        self.assertFalse(any(c.layer == 2 for c in fp))  # B is free under F-In2
        legacy = grid(12, None)
        fp = _footprint(legacy, cells, 1, edges=[(cells[0], cells[1])], net="N")
        self.assertTrue(any(c.layer == 2 for c in fp))

    def test_other_net_crosses_on_b_under_blind_via(self):
        """Net A must change from F.Cu (west) to In2.Cu (east) at column 4; net B
        runs straight along B.Cu right under that column, which a through via there
        would forbid."""
        for policy, crossing in ((BB, True), (None, False)):
            g = grid(9, policy, keepout=1)
            g.blocked[0, :, 5:] = True  # F.Cu only west of column 4
            g.blocked[1, :, :4] = True  # In2.Cu only east of it
            for j in range(9):
                if j != 4:
                    g.blocked[0, j, 4] = g.blocked[1, j, 4] = True
            access = {"A": [Cell(0, 0, 4), Cell(1, 8, 4)], "B": [Cell(2, 4, 0), Cell(2, 4, 8)]}
            res = route(g, access, max_iters=6, via_keepout=1)
            self.assertFalse(res.unrouted, policy)
            straight = all(c.i == 4 for c in res.nets["B"].cells)
            self.assertEqual(straight, crossing, policy)
            a = res.nets["A"]
            if policy is not None:
                spans = [s for ss in a.via_spans.values() for s in ss]
                self.assertEqual([(s.top, s.bottom) for s in spans], [("F.Cu", "In2.Cu")])
            else:
                self.assertEqual(a.via_spans, {})

    def test_all_through_model_matches_legacy(self):
        """A via model whose only kind is through routes exactly like no model."""
        through_only = dict(allowed=[THROUGH], layers=list(SIX), gaps_mm=GAPS6, sizes={})
        results = []
        for model in (None, through_only):
            g = RouteGrid(14, 10, 0.5, layers=GRID_LAYERS, clearance=0.2, via_radius=0.3)
            if model:
                g.via_model = GridVias(model, GRID_LAYERS, 0.5, 0.2, (0.6, 0.3), 1)
            for j in range(2, 8):
                g.blocked[0, j, 10] = True
            access = {
                "A": [Cell(0, 1, 1), Cell(0, 26, 18)],
                "B": [Cell(0, 1, 18), Cell(2, 26, 1)],
                "C": [Cell(1, 4, 9), Cell(0, 22, 9)],
            }
            res = route(g, access, max_iters=6, via_keepout=1, via_cost=6.0)
            results.append({n: (rn.cells, rn.segments, rn.vias) for n, rn in res.nets.items()})
        self.assertEqual(results[0], results[1])


class FixedSpans(unittest.TestCase):
    def test_fixed_blind_via_reserves_its_span(self):
        from pnr.route.detail.fixed import reserve_fixed_copper

        copper = dict(
            frame="engine-mm-y-up",
            tracks=[],
            vias=[
                dict(
                    net="S",
                    xy=[5.0, 5.0],
                    diameter_mm=0.6,
                    drill_mm=0.3,
                    type="blind",
                    layers=["F.Cu", "In2.Cu"],
                ),
                dict(
                    net="G",
                    xy=[10.0, 5.0],
                    diameter_mm=0.3,
                    drill_mm=0.1,
                    type="micro",
                    layers=["In4.Cu", "B.Cu"],
                ),
                dict(net="T", xy=[15.0, 5.0], diameter_mm=0.6, drill_mm=0.3, type="through"),
            ],
        )
        g = RouteGrid(20, 10, 0.25, layers=GRID_LAYERS, clearance=0.2, via_radius=0.3)
        reserve_fixed_copper(g, copper)
        i, j = g.cell_of(5.0, 5.0)
        self.assertTrue(g.blocked[0, j, i] and g.blocked[1, j, i])
        self.assertFalse(g.blocked[2, j, i])  # B.Cu is free under F-In2
        self.assertTrue(g.via_blocked[2, j, i])  # its drill still spaces new vias
        i, j = g.cell_of(10.0, 5.0)
        self.assertEqual([bool(g.blocked[la, j, i]) for la in range(3)], [False, False, True])
        i, j = g.cell_of(15.0, 5.0)
        self.assertTrue(all(g.blocked[la, j, i] for la in range(3)))
        with self.assertRaises(ValueError):
            reserve_fixed_copper(g, dict(copper, vias=[dict(copper["vias"][0], layers=None)]))


class BoardSpans(unittest.TestCase):
    def routed(self, policy, banned=()):
        g = sample()
        g.stack = typed("SGSGPS", zones={"In3.Cu": ["GND"]})
        c, rules = compiled(g, 6, planes={"GND": "In1.Cu", "VCC": "In4.Cu"})
        p = resolve(policy, SIX, gaps=GAPS6, banned=banned)
        if p:
            rules["via_policy"] = p
        return g, route_board(g, c, rules, pitch=0.25)

    def test_through_policy_has_no_spans(self):
        _, b = self.routed(None)
        self.assertEqual(b.via_spans, [])
        self.assertFalse(b.result.unrouted)

    def test_spans_match_track_layers(self):
        """Every non-through via's span joins the layers its net's copper uses at
        that point, and no two same-net barrels there share a layer."""
        for policy, banned in ((BB, {MICRO}), (HDI, ())):
            g, b = self.routed(policy, banned)
            self.assertFalse(b.result.unrouted)
            kinds = Counter(s[5] for s in b.via_spans)
            self.assertTrue(kinds, policy)
            if MICRO in policy["allowed"]:
                self.assertIn(MICRO, kinds)
            else:
                self.assertNotIn(MICRO, kinds)
            names = list(SIX)
            sites = {}
            for net, x, y, top, bottom, kind in b.via_spans:
                self.assertIn((net, x, y), set(b.vias))
                t, z = names.index(top), names.index(bottom)
                self.assertLess(t, z)
                if kind == MICRO:
                    self.assertEqual(z, t + 1)
                    self.assertTrue(t == 0 or z == 5)
                sites.setdefault((net, x, y), []).append((t, z))
                for tnet, layer, a, e, _w in b.tracks:
                    if tnet == net and any(math.dist(p, (x, y)) < 1e-6 for p in (a, e)):
                        # A track ending on the via is on one of its layers (or the
                        # via joins it through a plane, never for a signal).
                        self.assertTrue(
                            any(
                                names.index(layer) in range(s, w + 1) for s, w in sites[(net, x, y)]
                            )
                            or len(sites[(net, x, y)]) < 1,
                            (net, layer, top, bottom),
                        )
            for spans in sites.values():
                spans.sort()
                for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                    self.assertLess(a1, b0)  # disjoint: two holes, no shared layer

    def test_vias_keep_their_clearances(self):
        """Holes keep their spacing whatever the spans; two nets' vias that share a
        grid layer keep copper clearance there (at their own sizes)."""
        for policy, banned in ((BB, {MICRO}), (HDI, ())):
            g, b = self.routed(policy, banned)
            names = list(SIX)
            routed = {"F.Cu", "In2.Cu", "B.Cu"}
            spans = {}
            for net, x, y, top, bottom, kind in b.via_spans:
                spans.setdefault((net, x, y), []).append((top, bottom, kind))
            vias = []
            for key in b.vias:
                for top, bottom, kind in spans.get(key) or [("F.Cu", "B.Cu", THROUGH)]:
                    t, z = names.index(top), names.index(bottom)
                    layers = {n for n in names[t : z + 1] if n in routed}
                    radius = 0.15 if kind == MICRO else 0.3
                    drill = 0.1 if kind == MICRO else 0.3
                    vias.append((key, layers, radius, drill))
            for k, (a, la, ra, da) in enumerate(vias):
                for b2, lb, rb, db in vias[k + 1 :]:
                    d = math.dist(a[1:], b2[1:])
                    if d < 1e-6 and a[0] == b2[0]:
                        continue  # one barrel, merged
                    self.assertGreaterEqual(d, (da + db) / 2 + 0.25 - 1e-6, (a, b2))
                    if a[0] != b2[0] and la & lb:
                        self.assertGreaterEqual(d, ra + rb + 0.2 - 1e-6, (a, b2))

    def test_every_gnd_plane_layer_connected(self):
        """GND has two planes (In1, In3): blind or micro drops reach In1, and some
        reach In3 (deeper drops or buried stitches), at least two each."""
        for policy, banned in ((BB, {MICRO}), (HDI, ())):
            g, b = self.routed(policy, banned)
            conn = b.escape_diagnostics["plane_layer_connections"]["GND"]
            self.assertGreaterEqual(conn["In1.Cu"], 2)
            self.assertGreaterEqual(conn["In3.Cu"], 2)
            reach = Counter()
            names = list(SIX)
            spans = {(s[0], s[1], s[2]): s for s in b.via_spans}
            for net, x, y in b.vias:
                if net != "GND":
                    continue
                s = spans.get((net, x, y))
                t, z = (0, 5) if s is None else (names.index(s[3]), names.index(s[4]))
                for layer in ("In1.Cu", "In3.Cu"):
                    reach[layer] += t <= names.index(layer) <= z
            self.assertGreaterEqual(reach["In3.Cu"], 2)
            self.assertGreaterEqual(reach["In1.Cu"], 2)

    def test_vcc_drops_are_blind_to_in4(self):
        g, b = self.routed(BB, {MICRO})
        vcc = [s for s in b.via_spans if s[0] == "VCC"]
        self.assertTrue(vcc)
        self.assertTrue(all((s[3], s[4], s[5]) == ("F.Cu", "In4.Cu", BLIND) for s in vcc))


if __name__ == "__main__":
    unittest.main()
