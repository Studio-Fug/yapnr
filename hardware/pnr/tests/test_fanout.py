"""BGA fanout generator (pnr.fanout): lattice, fit arithmetic, assignment, spec."""

import json
import math
import unittest

from pnr.fanout import FanoutError, parse, parse_all, plan
from pnr.fanout.geom import Land, Pose, segment_segment
from pnr.fanout.lattice import PadLand, infer
from pnr.fanout.sites import Model, Obstacles
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

PITCH = 0.65
LAND = 0.32
FAB = {
    "track_width_mm": 0.1,
    "clearance_mm": 0.1,
    "via_diameter_mm": 0.4,
    "via_drill_mm": 0.2,
    "hole_clearance_mm": 0.1524,
    "edge_clearance_mm": 0.3,
    "min_through_drill_mm": 0.15,
    "via_annular_mm": 0.0762,
    "min_track_width_mm": 0.1,
    "smd_pad_clearance_mm": 0.1,
    "hole_to_hole_mm": 0.28,
    "via_to_smd_pad_mm": 0.1,
    "min_via_diameter_mm": 0.31,
}
GROUND = dict(name="ground", diameter_mm=0.35, drill_mm=0.15, nets=["GND"], sites=["interstitial"])
DEFAULT = dict(name="default", diameter_mm=0.4, drill_mm=0.2, nets=[], sites=["vacant", "outside"])
ROWS = "ABCDEFGHJKLMNPRTUVWY"


def ball(r, c):
    return ROWS[r] + str(c + 1)


def array(n, vacant=lambda r, c: False):
    """An n x n 0.65 mm array in the footprint frame (A1 top left, y up)."""
    half = (n - 1) / 2
    return {
        ball(r, c): ((c - half) * PITCH, (half - r) * PITCH)
        for r in range(n)
        for c in range(n)
        if not vacant(r, c)
    }


def ufbga201(r, c):
    """KiCad's UFBGA-201 15x15: ring 4 vacant around a 5x5 centre (176+25 balls)."""
    k = min(r, c, 14 - r, 14 - c)
    return k == 4


def lands(positions, nets=None):
    nets = nets or {}
    return [
        PadLand(name, nets.get(name, "N_" + name), xy, (LAND, LAND), LAND / 2)
        for name, xy in sorted(positions.items())
    ]


def board(positions, nets, *, pos=(20.0, 20.0), rot=0.0, targets=True):
    """U1 with ``positions`` and one target pad per signal net (degree 2)."""
    pads = [
        Pad(name=n, net=nets.get(n, ""), offset=xy, size=(LAND, LAND), land_corner=LAND / 2)
        for n, xy in sorted(positions.items())
    ]
    comps = [Component("U1", "bga", pos, rot, "top", (12, 12), (12, 12), locked=True, pads=pads)]
    pins = {}
    for n in sorted(positions):
        if nets.get(n):
            pins.setdefault(nets[n], []).append(("U1", n))
    k = 0
    for net in sorted(pins):
        if targets and net.startswith("S"):
            ref = "T%d" % k
            k += 1
            comps.append(
                Component(
                    ref,
                    "t",
                    (2.0 + (k % 18) * 2.0, 2.0),
                    0.0,
                    "top",
                    (0.6, 0.6),
                    (0.6, 0.6),
                    pads=[Pad("1", net, (0, 0), (0.3, 0.3), land_corner=0.15)],
                )
            )
            pins[net].append((ref, "1"))
    nets_out = [Net(n, i + 1, p) for i, (n, p) in enumerate(sorted(pins.items()))]
    return BoardGraph("t", comps, nets_out, BoardOutline(40, 40))


def spec(refs=("U1",), **kw):
    s = dict(
        name="u1",
        ref="U1",
        via_classes=dict(
            ground=dict(diameter_mm=0.35, drill_mm=0.15, nets=["GND"], sites=["interstitial"]),
            default=dict(diameter_mm=0.4, drill_mm=0.2, sites=["vacant", "outside"]),
        ),
    )
    s.update(kw)
    return parse(s, list(refs))


def rules():
    return {
        "layers": 6,
        "fab": dict(FAB),
        "net_classes": [{"name": "gnd", "nets": ["GND"], "plane_layer": "In1.Cu"}],
    }


def run(graph, sp, layers=("F.Cu", "In2.Cu", "B.Cu")):
    nets = {n.name for n in graph.nets}
    return plan(
        graph,
        rules(),
        sp,
        grid_layers=list(layers),
        plane_nets={"GND"} & nets,
        signal_nets={n.name for n in graph.nets if n.name != "GND" and n.degree >= 2},
    )


def violations(p, graph, clr=0.1, h2h=0.28, v2p=0.1):
    """Every pair of foreign copper items of the plan closer than the rules allow,
    measured independently of the planner's stencils."""
    comp = graph.component("U1")
    pose = Pose(comp.pos, comp.rot)
    lands_ = [
        Land(pose.to_board(pad.offset), LAND / 2, LAND / 2, LAND / 2, pad.net, pad.name)
        for pad in comp.pads
    ]
    tracks = [(t[0], t[1], tuple(t[2]), tuple(t[3]), t[4]) for t in p["copper"]["tracks"]]
    vias = [(v[0], (v[1], v[2]), v[3], v[4]) for v in p["copper"]["vias"]]
    bad = []
    for i, (n1, l1, a1, b1, w1) in enumerate(tracks):
        for n2, l2, a2, b2, w2 in tracks[i + 1 :]:
            if n1 != n2 and l1 == l2:
                if segment_segment(a1, b1, a2, b2) < (w1 + w2) / 2 + clr - 1e-6:
                    bad.append(("track-track", n1, n2))
        for n2, c, d, _h in vias:
            if n1 != n2 and segment_segment(a1, b1, c, c) < w1 / 2 + d / 2 + clr - 1e-6:
                bad.append(("track-via", n1, n2))
        if l1 == "F.Cu":
            for land in lands_:
                if land.net != n1 and land.distance(a1, b1) < w1 / 2 + clr - 1e-6:
                    bad.append(("track-land", n1, land.name))
    for i, (n1, c1, d1, h1) in enumerate(vias):
        for n2, c2, d2, h2 in vias[i + 1 :]:
            gap = math.dist(c1, c2)
            if gap < 1e-9 and n1 == n2:
                continue
            if (n1 != n2 and gap < (d1 + d2) / 2 + clr - 1e-6) or gap - (h1 + h2) / 2 < h2h - 1e-6:
                bad.append(("via-via", n1, n2))
        for land in lands_:
            gap = land.distance(c1, c1)
            if gap < d1 / 2 + max(clr, v2p) - 1e-6:
                bad.append(("via-land", n1, land.name))
    return bad


class LatticeTest(unittest.TestCase):
    def test_ufbga201_vacant_ring(self):
        lat = infer(lands(array(15, ufbga201)))
        self.assertAlmostEqual(lat.px, PITCH)
        self.assertAlmostEqual(lat.py, PITCH)
        self.assertEqual((lat.cols, lat.rows), (15, 15))
        self.assertEqual(len(lat.balls), 201)
        self.assertEqual(len(lat.vacant()), 24)
        rings = lat.summary()["balls_per_ring"]
        self.assertEqual(rings, {"0": 56, "1": 48, "2": 40, "3": 32, "5": 16, "6": 8, "7": 1})

    def test_full_arrays_and_an_exposed_pad(self):
        for n in (13, 10):
            pads = lands(array(n))
            pads.append(PadLand("EP", "GND", (0.1, 0.0), (2.0, 2.0), 0.0))
            lat = infer(pads)
            self.assertEqual((lat.cols, lat.rows, len(lat.balls)), (n, n, n * n))
            self.assertEqual([p.name for p in lat.others], ["EP"])
            self.assertEqual(lat.ring(0, 0), 0)
            self.assertEqual(lat.ring(n // 2, n // 2), (n - 1) // 2)

    def test_site_kinds(self):
        lat = infer(lands(array(5, lambda r, c: (r, c) == (2, 2))))
        self.assertEqual(lat.kind(0, 0), "ball")
        self.assertEqual(lat.kind(4, 4), "vacant")
        self.assertEqual(lat.kind(1, 1), "interstitial")
        self.assertEqual(lat.kind(1, 0), "channel")
        self.assertEqual(lat.kind(-1, 0), "outside")


class FitTest(unittest.TestCase):
    """The 0.65 mm / 0.32 mm land arithmetic of the design (stage 2 section 5)."""

    def model(self, vacant=()):
        positions = array(5, lambda r, c: (r, c) in vacant)
        lat = infer(lands(positions))
        obs = Obstacles()
        for p in lands(positions):
            obs.lands.append(Land(p.centre, LAND / 2, LAND / 2, LAND / 2, p.net, p.name))
        return Model(
            lat,
            ["F.Cu", "In2.Cu"],
            obs,
            clearance=0.1,
            via_to_pad=0.1,
            hole_to_hole=0.28,
            edge_clearance=0.3,
            hole_to_edge=None,
            via_classes=[dict(GROUND, sites=["interstitial", "vacant"]), DEFAULT],
            widths=[0.1],
        )

    def test_interstitial_fits_035_not_040(self):
        m = self.model()
        self.assertEqual(m.via_blockers((3, 3), 0, "X"), frozenset())
        # 0.40/0.20 misses an interstitial site by 0.4 um (default sites exclude it).
        m.via_classes[1] = dict(DEFAULT, sites=["interstitial"])
        m._via_cache.clear()
        self.assertTrue(m.via_blockers((3, 3), 1, "X"))

    def test_vacant_via_conflicts_with_its_interstitial_neighbours(self):
        m = self.model()
        other, _same = m.stencils[(("v", 1, 0), ("v", 0, 0))]
        for offset in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            self.assertIn(offset, other)
        self.assertNotIn((2, 2), m.stencils[(("v", 0, 0), ("v", 0, 0))][0])

    def test_a_wider_class_clearance_closes_the_channel_beside_it(self):
        m = self.model()
        m.net_clearance = {"N_E2": 0.15}  # the ball east of the channel (0.115 mm away)
        m.clearances = [0.1, 0.15]
        m._edge_cache.clear()
        # A 0.10 track of a 0.10 net between E1 and E2 must keep 0.15 from E2 land.
        self.assertIn("N_E2", m.edge_blockers(0, (1, 0), 1, 0.1, 0.1))
        self.assertNotIn("N_E1", m.edge_blockers(0, (1, 0), 1, 0.1, 0.1))
        # The same channel between two 0.10 nets is free.
        self.assertEqual(m.edge_blockers(0, (5, 0), 1, 0.1, 0.1), frozenset())

    def test_channel_holds_one_track(self):
        m = self.model()
        # A track along the channel between two balls (x = half a pitch) clears both.
        self.assertEqual(m.edge_blockers(0, (1, 0), 1, 0.1), frozenset())
        # Two tracks a half pitch apart are legal; one cell apart they are not.
        edges = m.stencils[(("e", 1, 0, 0), ("e", 1, 0, 0))][0]
        self.assertNotIn((1, 0), edges)
        self.assertIn((0, 0), edges)
        # A track from a ball (lattice node 0, 0: E1, the bottom-left ball) is
        # blocked by that ball's net only, which may use it.
        self.assertEqual(m.edge_blockers(0, (0, 0), 0, 0.1), frozenset({"N_E1"}))
        self.assertTrue(m.legal(m.edge_blockers(0, (0, 0), 0, 0.1), "N_E1"))


class PlanTest(unittest.TestCase):
    def nets(self, positions, signals):
        nets = {}
        for n in positions:
            nets[n] = "S_" + n if n in signals else "GND"
        return nets

    def test_ufbga_plan_is_complete_legal_and_deterministic(self):
        positions = array(15, ufbga201)
        signals = {
            b
            for b in positions
            if min(ROWS.index(b[0]), int(b[1:]) - 1, 14 - ROWS.index(b[0]), 15 - int(b[1:])) < 4
            and (ROWS.index(b[0]) + int(b[1:])) % 3 == 0
        }
        graph = board(positions, self.nets(positions, signals))
        first = run(graph, spec())
        second = run(graph, spec())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        d = first["diagnostics"]
        self.assertEqual(d["signals_escaped"], d["signals"])
        self.assertEqual(d["drops_placed"], d["drops"])
        self.assertEqual(violations(first, graph), [])
        # Every ground drop is a 0.35/0.15 via on an interstitial site.
        for name, row in first["terminals"].items():
            if row["kind"] == "drop":
                self.assertEqual(row["via_site"], "interstitial")
                self.assertEqual(row["via"][2:], [0.35, 0.15])

    def test_reserved_corridor_and_forbidden_exits(self):
        positions = array(9)
        signals = {b for b in positions if ROWS.index(b[0]) in (0, 1, 7, 8)}
        graph = board(positions, self.nets(positions, signals), rot=90.0)
        # Keep the copper off a 1 mm band beyond the west edge of the part frame.
        sp = spec(
            reserved=[{"rect": [-4.5, -4.0, -3.4, 4.0], "layers": ["*"]}],
            forbidden_exits=["east"],
        )
        p = run(graph, sp)
        pose = Pose((20.0, 20.0), 90.0)
        for net, layer, a, b, w in p["copper"]["tracks"]:
            for q in (a, b):
                x, _ = pose.to_local(tuple(q))
                self.assertFalse(-3.4 + w / 2 > x > -4.5 - w / 2, (net, q))
        for name, row in p["terminals"].items():
            if row.get("outward"):
                self.assertLess(row["outward"][0], 0.5, name)  # never east
        self.assertEqual(violations(p, graph), [])

    def test_fixed_pour_joins_its_balls_and_keeps_others_out(self):
        positions = array(5)
        nets = {n: "GND" if n in ("A1", "A2", "B1") else "S_" + n for n in positions}
        graph = board(positions, nets)
        # A GND pour over the north-west corner (A1, A2, B1) in the board frame.
        pour = [[18.3, 20.9], [19.7, 20.9], [19.7, 21.6], [18.3, 21.6]]
        fixed = dict(
            frame="engine-mm-y-up",
            tracks=[],
            vias=[],
            polygons=[dict(net="GND", layer="F.Cu", kind="zone", outline=pour)],
        )
        p = plan(
            graph,
            rules(),
            spec(),
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets={n.name for n in graph.nets if n.name != "GND"},
            fixed_copper=fixed,
        )
        joined = sorted(n for n, r in p["terminals"].items() if r["kind"] == "fixed")
        self.assertEqual(joined, ["A1", "A2"])  # B1 lies outside the pour
        from pnr.fanout.geom import segment_polygon

        for net, layer, a, b, w in p["copper"]["tracks"]:
            if layer == "F.Cu" and net != "GND":
                self.assertGreaterEqual(
                    segment_polygon(tuple(a), tuple(b), pour), w / 2 + 0.1 - 1e-6
                )

    def test_a_class_keeps_its_nets_on_its_layers(self):
        positions = array(7)
        signals = {b for b in positions if ROWS.index(b[0]) in (2, 3)}
        graph = board(positions, self.nets(positions, signals))
        sp = spec(
            via_classes={
                "ground": {
                    "diameter_mm": 0.35,
                    "drill_mm": 0.15,
                    "nets": ["GND"],
                    "sites": ["interstitial"],
                },
                "outer": {
                    "diameter_mm": 0.4,
                    "drill_mm": 0.2,
                    "nets": ["S_*"],
                    "sites": ["vacant", "outside", "interstitial"],
                    "layers": ["F.Cu", "B.Cu"],
                },
            }
        )
        sp = __import__("pnr.fanout", fromlist=["expand_nets"]).expand_nets(
            sp, [n.name for n in graph.nets]
        )
        p = run(graph, sp)
        self.assertEqual(p["diagnostics"]["signals_escaped"], p["diagnostics"]["signals"])
        for net, layer, *_ in p["copper"]["tracks"]:
            if net.startswith("S_"):
                self.assertIn(layer, ("F.Cu", "B.Cu"))

    def test_failed_pad_has_a_reason(self):
        positions = array(5)
        signals = {"C3", "A1"}
        graph = board(positions, self.nets(positions, signals))
        p = run(graph, spec(forbidden_exits=["north", "south", "east", "west"]))
        for name in ("A1", "C3"):
            self.assertEqual(p["terminals"][name]["kind"], "failed")
            self.assertIn("no legal path", p["terminals"][name]["reason"])
        self.assertEqual(p["diagnostics"]["signals_escaped"], 0)
        self.assertEqual(p["diagnostics"]["drops_placed"], p["diagnostics"]["drops"])


class BottomSitesTest(unittest.TestCase):
    def test_sites_clear_the_vias_and_reach_their_nets(self):
        # A peripheral array (rings 0-3), its inner ring alternating supply and ground.
        positions = array(15, lambda r, c: min(r, c, 14 - r, 14 - c) > 3)
        nets = {}
        for name in positions:
            r, c = ROWS.index(name[0]), int(name[1:]) - 1
            k = min(r, c, 14 - r, 14 - c)
            nets[name] = ("GND" if (r + c) % 2 else "VCC") if k == 3 else ""
        graph = board(positions, nets, targets=False)
        for ref in ("C1", "C2"):
            pads = [
                Pad("1", "VCC", (-0.48, 0.0), (0.56, 0.62), land_corner=0.14),
                Pad("2", "GND", (0.48, 0.0), (0.56, 0.62), land_corner=0.14),
            ]
            graph.components.append(
                Component(ref, "c", (0, 0), 0.0, "top", (1.86, 0.94), (1.86, 0.94), pads=pads)
            )
        sp = spec(
            ("U1", "C1", "C2"),
            via_classes={
                "ground": {
                    "diameter_mm": 0.35,
                    "drill_mm": 0.15,
                    "nets": ["GND", "VCC"],
                    "sites": ["interstitial"],
                }
            },
            bottom_sites={"parts": ["C1", "C2"], "max_stub_mm": 0.6},
        )
        r = rules()
        p = plan(
            graph,
            r,
            sp,
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND", "VCC"},
            signal_nets=set(),
        )
        found = p["bottom"]
        self.assertEqual(sorted(found["sites"]), ["C1", "C2"], found["unplaced"])
        vias = [((v[1], v[2]), v[3]) for v in p["copper"]["vias"]]
        boxes = []
        for ref, site in found["sites"].items():
            pose = Pose(site["at"], site["rot"])
            self.assertEqual(site["side"], "bottom")
            for pad in graph.component(ref).pads:
                c = pose.to_board((pad.offset[0], -pad.offset[1]))
                swap = int(round(site["rot"])) % 180 == 90
                w, h = (pad.size[1], pad.size[0]) if swap else pad.size
                land = Land(c, w / 2, h / 2, 0.14)
                for at, d in vias:
                    self.assertGreaterEqual(land.distance(at, at) - d / 2, 0.1 - 1e-6)
                self.assertLessEqual(site["stubs"][pad.name], 0.6 + 1e-9)
            boxes.append(site["at"])
        self.assertGreater(math.dist(*boxes), 0.9)
        self.assertTrue(found["keepouts"])


class SpecTest(unittest.TestCase):
    def test_errors_name_the_key(self):
        with self.assertRaisesRegex(FanoutError, "unknown key"):
            parse(dict(ref="U1", vias=1), ["U1"])
        with self.assertRaisesRegex(FanoutError, "drill must be smaller"):
            parse(
                dict(ref="U1", via_classes={"default": {"diameter_mm": 0.2, "drill_mm": 0.3}}),
                ["U1"],
            )
        with self.assertRaisesRegex(FanoutError, "forbidden_exits"):
            parse(dict(ref="U1", forbidden_exits=["up"]), ["U1"])
        with self.assertRaisesRegex(FanoutError, "unknown component"):
            parse(dict(ref="U9"), ["U1"])
        with self.assertRaisesRegex(FanoutError, "at most one fanout"):
            parse_all([dict(ref="U1", name="a"), dict(ref="U1", name="b")], ["U1"])

    def test_fab_check(self):
        from pnr.fanout import check

        sp = spec(via_classes={"default": {"diameter_mm": 0.3, "drill_mm": 0.1}})
        with self.assertRaisesRegex(FanoutError, "min_through_drill"):
            check(sp, rules(), ["F.Cu", "B.Cu"])
        sp = spec(escape_layers=["In7.Cu"])
        with self.assertRaisesRegex(FanoutError, "not a routing layer"):
            check(sp, rules(), ["F.Cu", "B.Cu"])

    def test_rules_keep_their_bytes_without_a_fanout(self):
        from pnr.constraints import compile_constraints, compile_routing_rules

        doc = {"schema": "v0", "board": {"outline": {"w": 10, "h": 10}}}
        c = compile_constraints(doc, ["U1"])
        self.assertEqual(c.fanouts, [])
        self.assertNotIn("fanouts", compile_routing_rules(c, ["GND"]))
        doc["fanout"] = [
            dict(
                ref="U1", via_classes={"g": {"diameter_mm": 0.35, "drill_mm": 0.15, "nets": ["G*"]}}
            )
        ]
        rules_ = compile_routing_rules(compile_constraints(doc, ["U1"]), ["GND", "SIG"])
        self.assertEqual(rules_["fanouts"][0]["via_classes"][0]["nets"], ["GND"])


if __name__ == "__main__":
    unittest.main()
