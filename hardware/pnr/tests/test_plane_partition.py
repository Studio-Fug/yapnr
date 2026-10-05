"""Rails sharing a plane layer (pnr.plane_partition, the plane_partition section)."""

import math
import unittest

import numpy as np

from pnr.plane_partition import _CACHE, Terminal, _Grid, partition, regions_from_rows
from pnr.stack import PlaneAccess, Region, resolve

ENTRY = dict(
    layer="In2.Cu",
    nets=["A", "B"],
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


def pad(name, at):
    return Terminal(name, "pad", at, 0.8)


def via(name, at):
    return Terminal(name, "via", at, 0.175)


TERMS = {
    "A": [pad("A1", (2.0, 2.0)), pad("A2", (18.0, 2.0)), via("A3", (10.0, 5.0))],
    "B": [pad("B1", (2.0, 10.0)), pad("B2", (18.0, 10.0)), via("B3", (10.0, 7.0))],
}
FOREIGN = [((x, 6.0), 0.3) for x in (6.0, 8.0, 12.0, 14.0)]


def run(entry=ENTRY, terms=TERMS, blocked=FOREIGN, **kwargs):
    _CACHE.clear()
    return partition(
        dict(entry), width=20.0, height=12.0, terminals=terms, blocked=blocked, **kwargs
    )


def inside(region, p):
    from pnr.stack import _inside

    if region.outline is None:
        return True
    return _inside(region.outline, p) and not any(_inside(h, p) for h in region.holes)


def coverage(regions, net, step=0.05):
    g = _Grid(20.0, 12.0, step)
    mask = g.zeros()
    for r in regions:
        if r.net == net:
            rings = [r.outline] + list(r.holes)
            mask |= g.polygon(rings)
    return mask


class PartitionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.part = run(currents={"A": 2.0, "B": 1.0})

    def test_every_rail_is_one_connected_territory(self):
        nets = self.part.report["nets"]
        for net in ("A", "B"):
            self.assertEqual(nets[net]["reached"], 3, nets[net])
            self.assertEqual(nets[net]["components"], 1)
            self.assertEqual(nets[net]["unreached"], [])
        self.assertEqual(self.part.report["order"], ["A", "B"])  # higher current first
        for net, terms in TERMS.items():
            mine = [r for r in self.part.regions if r.net == net]
            self.assertEqual(len(mine), 1)
            for t in terms:
                self.assertTrue(inside(mine[0], t.at), (net, t.name))

    def test_territories_keep_the_split_gap(self):
        self.assertGreaterEqual(self.part.report["gap_min_mm"], 0.3 - 1e-6)
        a = coverage(self.part.regions, "A")
        b = coverage(self.part.regions, "B")
        self.assertFalse((a & b).any())
        self.assertGreater(a.sum(), 0)
        self.assertGreater(b.sum(), 0)

    def test_the_trunk_keeps_its_width(self):
        nets = self.part.report["nets"]
        for net in ("A", "B"):
            self.assertGreaterEqual(nets[net]["width_mm"], 1.0)
            self.assertGreaterEqual(nets[net]["core_min_mm"], 1.0 - 0.1 - 1e-9)

    def test_foreign_copper_is_not_territory(self):
        # The foreign vias' centres are in no rail's copper unless a hole was filled
        # over them (KiCad's fill clears its antipad): never both rails.
        a = coverage(self.part.regions, "A")
        b = coverage(self.part.regions, "B")
        g = _Grid(20.0, 12.0, 0.05)
        for c, _r in FOREIGN:
            i, j = g.cell(c)
            self.assertFalse(a[j, i] and b[j, i])

    def test_deterministic_and_cached(self):
        again = run(currents={"A": 2.0, "B": 1.0})
        self.assertEqual(again.rows(), self.part.rows())
        self.assertEqual(again.report["inputs_sha256"], self.part.report["inputs_sha256"])
        rows = self.part.rows()
        self.assertEqual([r.net for r in regions_from_rows(rows)], [r["net"] for r in rows])

    def test_fill_takes_the_rest_under_the_rails(self):
        part = run(entry=dict(ENTRY, fill="GND"))
        fill = [r for r in part.regions if r.net == "GND"]
        self.assertEqual(len(fill), 1)
        self.assertIsNone(fill[0].outline)
        self.assertEqual(fill[0].priority, 0)
        self.assertTrue(all(r.priority >= 1 for r in part.regions if r.net != "GND"))


class OverlapTest(unittest.TestCase):
    def test_a_terminal_inside_another_ones_disc_is_reached(self):
        # A planned via inside a pad's reach disc (a decoupling cap beside its ball's
        # via): the tree that reaches the disc reaches the via too.
        terms = dict(TERMS, A=TERMS["A"] + [via("A4", (2.4, 2.3))])
        part = run(terms=terms)
        self.assertEqual(part.report["nets"]["A"]["reached"], 4)
        self.assertEqual(part.report["nets"]["A"]["unreached"], [])


class WidthTest(unittest.TestCase):
    def test_current_widens_the_trunk(self):
        from pnr.electrical import current_width

        part = run(currents={"A": 6.0})
        want = current_width(6.0, 1.0, 10.0, external=False)
        self.assertGreater(want, 1.0)
        self.assertAlmostEqual(part.report["nets"]["A"]["width_mm"], want, places=3)

    def test_an_ir_budget_widens_the_trunk(self):
        part = run(budgets_mohm={"A": 4.0})
        info = part.report["nets"]["A"]
        self.assertGreater(info["width_budget_mm"], 1.0)
        self.assertAlmostEqual(info["width_mm"], info["width_budget_mm"], places=3)


class UnreachedTest(unittest.TestCase):
    def test_a_walled_in_terminal_is_reported(self):
        ring = [
            ((15.0 + 1.6 * math.cos(a), 9.0 + 1.6 * math.sin(a)), 0.35)
            for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)
        ]
        terms = dict(TERMS, B=TERMS["B"] + [pad("B4", (15.0, 9.0))])
        part = run(terms=terms, blocked=FOREIGN + ring)
        info = part.report["nets"]["B"]
        self.assertEqual(info["reached"], 3)
        self.assertEqual([u["name"] for u in info["unreached"]], ["B4"])

    def test_a_tree_passes_no_neck_a_zone_cannot_fill(self):
        # One rail, two vias either side of a wall with a 0.22 mm slot: a 0.15 mm
        # zone cannot fill the slot (its copper keeps half its width from the
        # wall's cells), so with fill_min_mm the far via is unreached.
        entry = dict(ENTRY, nets=["A"], min_width_mm=0.15)
        terms = {"A": [via("A1", (1.0, 2.0)), via("A2", (9.0, 2.0))]}

        def wall(slot):
            low = [[4.9, -1.0], [5.1, -1.0], [5.1, 2.0 - slot / 2], [4.9, 2.0 - slot / 2]]
            high = [[4.9, 2.0 + slot / 2], [5.1, 2.0 + slot / 2], [5.1, 5.0], [4.9, 5.0]]
            return [([low], frozenset()), ([high], frozenset())]

        def reached(slot, fill_min):
            _CACHE.clear()
            part = partition(
                dict(entry),
                width=10.0,
                height=4.0,
                terminals=terms,
                blocked=[],
                blocked_polygons=wall(slot),
                fill_min_mm=fill_min,
            )
            return part.report["nets"]["A"]["reached"]

        self.assertEqual(reached(0.22, 0.0), 2)  # the raster alone passes it
        self.assertEqual(reached(0.22, 0.15), 1)
        self.assertEqual(reached(0.6, 0.15), 2)


class HardWidthTest(unittest.TestCase):
    """min_width_mm is a hard limit outside a rail's own terminal discs: a trunk that
    has to cross a field of foreign vias (a ball array's) goes round it where a way
    that wide exists, else joins the far terminal through the field and reports it
    necked, with the neck's width and place."""

    ENTRY = dict(ENTRY, nets=["A"], min_width_mm=1.0)
    TERMS = {"A": [via("A1", (1.0, 3.0)), via("A2", (11.0, 3.0))]}

    @staticmethod
    def field(top):
        # Foreign vias (0.25 mm with their clearance) in a column at x = 6, 0.9 mm
        # apart: 0.4 mm of copper between two, from y = 0.3 up to ``top``.
        ys = np.arange(0.3, top + 1e-9, 0.9)
        return [((6.0, float(y)), 0.25) for y in ys]

    def solve(self, top, **kwargs):
        _CACHE.clear()
        return partition(
            dict(self.ENTRY, **kwargs),
            width=12.0,
            height=6.0,
            terminals=self.TERMS,
            blocked=self.field(top),
            fill_min_mm=0.15,
        )

    def test_a_way_round_the_field_is_taken(self):
        part = self.solve(3.0)  # the field stops at y 3.0: 2.7 mm of free board above it
        info = part.report["nets"]["A"]
        self.assertEqual(info["reached"], 2)
        self.assertEqual(info["necked"], [])
        self.assertEqual(info["joined_narrow"], [])
        self.assertEqual(info["status"], "ok")
        self.assertGreaterEqual(info["way_min_mm"], 1.0 - 0.1 - 1e-9)

    def test_a_trunk_through_the_field_is_necked(self):
        part = self.solve(5.7)  # the field spans the board: no 1 mm way exists
        info = part.report["nets"]["A"]
        self.assertEqual(info["reached"], 2)  # joined (a zone fills 0.4 mm) ...
        self.assertEqual(info["joined_narrow"], ["A2"])
        self.assertEqual(info["status"], "necked")  # ... and reported, never silent
        (row,) = info["necked"]
        self.assertEqual(row["name"], "A2")
        self.assertLess(row["width_mm"], 0.7)  # the 0.4 mm gaps, to a raster cell
        self.assertAlmostEqual(row["neck_at"][0], 6.0, delta=0.3)
        self.assertEqual(info["reached_at_width"], 1)
        self.assertTrue(any("under min_width_mm" in w for w in info["warnings"]))

    def test_neck_mm_lets_a_trunk_narrow_near_its_terminals(self):
        # The field 0.6 mm from A2: within neck_mm 1.5 of it the trunk may narrow.
        _CACHE.clear()
        terms = {"A": [via("A1", (1.0, 3.0)), via("A2", (6.6, 3.0))]}
        part = partition(
            dict(self.ENTRY, neck_mm=1.5),
            width=12.0,
            height=6.0,
            terminals=terms,
            blocked=self.field(5.7),
            fill_min_mm=0.15,
        )
        info = part.report["nets"]["A"]
        self.assertEqual(info["status"], "ok", info)
        self.assertEqual(info["joined_narrow"], [])

    def test_the_cores_cover_the_claimed_width(self):
        # A 2 A rail is widened past min_width_mm; core_no_vias keeps other nets' vias
        # off all of its claimed copper, not only the minimum width's core.
        _CACHE.clear()
        terms = {"A": [pad("A1", (2.0, 3.0)), pad("A2", (10.0, 3.0))]}
        part = partition(
            dict(self.ENTRY, h_mm=0.1),
            width=12.0,
            height=6.0,
            terminals=terms,
            blocked=[],
            currents={"A": 2.0},
        )
        w = part.report["nets"]["A"]["width_mm"]
        self.assertGreater(w, 1.5)
        points, half = part.cores["A"]
        ys = [y for x, y in points if abs(x - 6.0) < 0.06]
        self.assertGreaterEqual(max(ys) - min(ys) + 2 * half, w - 0.2)

    def test_core_keepouts_spare_other_pads_drop_sites(self):
        from types import SimpleNamespace

        from pnr.plane_partition import _core_keepouts

        masks = []
        grid = SimpleNamespace(
            nx=120,
            ny=60,
            pitch=0.1,
            width=12.0,
            height=6.0,
            nlayers=2,
            add_net_keepout=lambda track, via, allowed: masks.append((via, allowed)),
        )
        _CACHE.clear()
        terms = {"A": [pad("A1", (2.0, 3.0)), pad("A2", (10.0, 3.0))]}
        part = partition(dict(self.ENTRY), width=12.0, height=6.0, terminals=terms, blocked=[])
        _core_keepouts(grid, part, 0.35, spare=[(6.0, 3.2)], spare_reach=0.8)
        ((via, allowed),) = masks
        self.assertEqual(allowed, {"A"})
        self.assertFalse(via[0, 32, 60])  # (6.05, 3.25): another pad's drop site
        self.assertTrue(via[0, 30, 40])  # (4.05, 3.05): on the trunk


def land(name, at, size):
    return Terminal(name, "land", at, max(size) / 2, size)


class OuterRegionTest(unittest.TestCase):
    """An outer-layer partition inside a region with whole lands as terminals (a
    power stage's hot-rod lands, 0.25 x 1.82 mm at 0.5 mm pitch): each rail's pour
    owns its lands and joins them to its other parts' pads, inside the region."""

    REGION = [[0.5, 0.5], [7.5, 0.5], [7.5, 6.0], [0.5, 6.0]]
    ENTRY = dict(
        ENTRY,
        layer="F.Cu",
        nets=["SW", "GND", "VIN"],
        split_gap_mm=0.2,
        min_width_mm=0.25,
        region=REGION,
        terminals="pad",
        connect="solid",
    )
    HOT = (0.25, 1.82)
    TERMS = {
        "SW": [land("U2.1", (2.0, 3.0), HOT), land("U2.2", (2.5, 3.0), HOT)]
        + [land("L1.1", (2.25, 0.95), (1.2, 0.8))],
        "GND": [land("U2.3", (3.0, 3.0), HOT), land("U2.4", (3.5, 3.0), HOT)]
        + [land("C1.2", (3.25, 5.2), (0.6, 0.6))],
        "VIN": [land("U2.5", (4.0, 3.0), HOT), land("U2.6", (4.5, 3.0), HOT)]
        + [land("C2.1", (5.6, 3.0), (0.6, 0.9))],
    }
    # A foreign pad in the region (a feedback pin), with its clearance.
    FOREIGN = [([[[6.5, 1.0], [7.1, 1.0], [7.1, 1.8], [6.5, 1.8]]], frozenset())]

    @classmethod
    def setUpClass(cls):
        _CACHE.clear()
        cls.part = partition(
            dict(cls.ENTRY),
            width=10.0,
            height=8.0,
            terminals=cls.TERMS,
            blocked=[],
            blocked_polygons=cls.FOREIGN,
            fill_min_mm=0.15,
        )

    def cover(self, net, step=0.025):
        g = _Grid(10.0, 8.0, step)
        mask = g.zeros()
        for r in self.part.regions:
            if r.net == net:
                mask |= g.polygon([r.outline] + list(r.holes))
        return g, mask

    def test_every_rail_owns_its_lands_in_one_piece(self):
        for net, terms in self.TERMS.items():
            info = self.part.report["nets"][net]
            self.assertEqual(info["unreached"], [], (net, info))
            self.assertEqual(info["components"], 1)
            self.assertEqual(info["status"], "ok", info)
            g, mask = self.cover(net)
            for t in terms:
                w, h = t.size
                # The whole land (its rectangle, a raster cell inside its edges).
                for dx in (-w / 2 + 0.06, 0.0, w / 2 - 0.06):
                    for dy in (-h / 2 + 0.06, 0.0, h / 2 - 0.06):
                        i, j = g.cell((t.at[0] + dx, t.at[1] + dy))
                        self.assertTrue(mask[j, i], (net, t.name, dx, dy))

    def test_the_pours_keep_the_gap_and_stay_in_the_region(self):
        self.assertGreaterEqual(self.part.report["gap_min_mm"], 0.2 - 1e-6)
        masks = {n: self.cover(n)[1] for n in self.TERMS}
        nets = sorted(masks)
        for a in nets:
            for b in nets:
                if a < b:
                    self.assertFalse((masks[a] & masks[b]).any(), (a, b))
        g = _Grid(10.0, 8.0, 0.025)
        inside_region = g.polygon([self.REGION])
        for net, mask in masks.items():
            self.assertFalse((mask & ~inside_region).any(), net)
            i, j = g.cell((6.8, 1.4))  # the foreign pad
            self.assertFalse(mask[j, i], net)

    def test_regions_carry_the_connection(self):
        rows = self.part.rows()
        self.assertTrue(rows)
        self.assertTrue(all(r["connect"] == "solid" for r in rows))
        plain = run()
        self.assertTrue(all("connect" not in r for r in plain.rows()))

    def test_a_land_terminal_without_a_size_key_digests_as_before(self):
        t = via("A3", (10.0, 5.0))
        self.assertEqual(sorted(t.key()), ["at", "kind", "name", "radius"])
        self.assertIn("size", land("X", (0, 0), (1, 1)).key())


class PlaneAccessHolesTest(unittest.TestCase):
    def test_a_site_in_a_hole_is_not_in_the_region(self):
        rec = {
            "layers": [
                {"name": "F.Cu", "type": "signal", "zones": []},
                {"name": "In1.Cu", "type": "power", "zones": ["A", "B"]},
                {"name": "B.Cu", "type": "signal", "zones": []},
            ]
        }
        rules = dict(layers=3, net_classes=[])
        stack = resolve(rules, rec)
        square = ((0, 0), (10, 0), (10, 10), (0, 10))
        hole = ((4, 4), (4, 6), (6, 6), (6, 4))
        regions = [
            Region("In1.Cu", "A", 1, square, False, (hole,)),
            Region("In1.Cu", "B", 2, ((4.3, 4.3), (5.7, 4.3), (5.7, 5.7), (4.3, 5.7))),
        ]
        access = PlaneAccess(stack, regions, 0.2, 0.5)
        self.assertTrue(access.site_ok("A", (2.0, 2.0)))
        self.assertFalse(access.site_ok("A", (5.0, 5.0)))  # in the hole
        self.assertFalse(access.site_ok("A", (3.9, 5.0)))  # too near the hole's edge
        self.assertTrue(access.site_ok("B", (5.0, 5.0)))


class RouteTest(unittest.TestCase):
    """route_board with a partition of In2 between two rails (4 layers, GND on In1)."""

    @staticmethod
    def setup(partition=True):
        from pnr.constraints import compile_constraints, compile_routing_rules
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

        def part(ref, at, nets, pitch=1.0):
            pads = [
                Pad(str(k + 1), net, ((k - (len(nets) - 1) / 2) * pitch, 0.0), (0.6, 0.6))
                for k, net in enumerate(nets)
            ]
            return Component(
                ref, "c", at, 0.0, "top", (len(nets) * pitch, 1.0), (1.0, 1.0), pads=pads
            )

        comps = [
            part("J1", (2.0, 6.0), ["V1", "GND", "V2"]),
            part("C1", (8.0, 3.0), ["V1", "GND"]),
            part("C2", (16.0, 3.0), ["V1", "GND"]),
            part("C3", (8.0, 9.0), ["V2", "GND"]),
            part("C4", (16.0, 9.0), ["V2", "GND"]),
            part("R1", (12.0, 6.0), ["S", "S2"]),
            part("R2", (18.0, 6.0), ["S", "S2"]),
        ]
        pins = {}
        for c in comps:
            for p in c.pads:
                pins.setdefault(p.net, []).append((c.ref, p.name))
        nets = [Net(n, k + 1, p) for k, (n, p) in enumerate(sorted(pins.items()))]
        g = BoardGraph("partition", comps, nets, BoardOutline(20, 12))
        g.stack = {
            "layers": [
                {"name": "F.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
                {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "In2.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "B.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
            ]
        }
        doc = {
            "schema": "v0",
            "board": {"outline": {"w": 20, "h": 12}, "layers": 4},
            "fixed": {c.ref: {"at": list(c.pos), "rot": 0, "side": "top"} for c in comps},
            "net_class": {
                "gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"},
                "v1": {"nets": ["V1"], "plane_layer": "In2.Cu"},
                "v2": {"nets": ["V2"], "plane_layer": "In2.Cu"},
            },
        }
        if partition:
            doc["plane_partition"] = [
                {"layer": "In2.Cu", "nets": ["V*"], "min_width_mm": 1.0, "currents": {"V1": 2}}
            ]
        c = compile_constraints(doc, g.refs)
        return g, c, compile_routing_rules(c, [n.name for n in g.nets])

    def test_drops_land_in_their_territories(self):
        from pnr.route.detail.router import route_board

        g, c, rules = self.setup()
        r = route_board(g, c, rules, max_iters=4)
        rows = r.extras()["plane_regions"]
        self.assertEqual(sorted({row["net"] for row in rows}), ["V1", "V2"])
        report = r.escape_diagnostics["plane_partition"][0]
        self.assertEqual(report["nets"]["V1"]["unreached"], [])
        self.assertEqual(report["nets"]["V2"]["unreached"], [])
        regions = regions_from_rows(rows)
        for net in ("V1", "V2"):
            vias = [(x, y) for n, x, y in r.vias if n == net]
            self.assertEqual(len(vias), 3)  # one drop per pad
            mine = [reg for reg in regions if reg.net == net]
            for v in vias:
                self.assertTrue(any(inside(reg, v) for reg in mine), (net, v))
        self.assertNotIn("V1", r.failure_sites)
        self.assertTrue(r.grid.net_keepouts)  # the trunk cores
        self.assertNotIn("S", r.result.unrouted)

    def test_a_layer_not_typed_power_is_not_partitioned(self):
        from pnr.route.detail.router import route_board

        g, c, rules = self.setup()
        g.stack["layers"][2]["type"] = "mixed"
        r = route_board(g, c, rules, max_iters=2)
        self.assertNotIn("plane_regions", r.extras())

    def test_identity_without_the_section(self):
        from pnr.route.detail.router import route_board

        g, c, rules = self.setup(partition=False)
        self.assertNotIn("plane_partition", rules)
        r = route_board(g, c, rules, max_iters=4)
        self.assertNotIn("plane_regions", r.extras())
        self.assertNotIn("plane_partition", r.escape_diagnostics)


if __name__ == "__main__":
    unittest.main()
