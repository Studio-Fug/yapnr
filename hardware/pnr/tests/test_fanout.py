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

    def model(self, vacant=(), **kw):
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
            **kw,
        )

    def test_a_via_hole_keeps_the_fab_hole_clearance_to_other_copper(self):
        # 0.35/0.15 between 0.32 mm balls: its hole edge is 0.2246 mm from each land.
        self.assertEqual(self.model(hole_clearance=0.2).via_blockers((3, 3), 0, "X"), frozenset())
        blocked = self.model(hole_clearance=0.25).via_blockers((3, 3), 0, "X")
        self.assertEqual(len(blocked), 4)  # the four balls around the site
        # Two such vias a pitch apart: copper 0.30 mm, hole to copper 0.40 mm apart.
        other, _same = self.model(hole_clearance=0.38).stencils[(("v", 0, 0), ("v", 0, 0))]
        self.assertNotIn((2, 0), other)
        other, _same = self.model(hole_clearance=0.41).stencils[(("v", 0, 0), ("v", 0, 0))]
        self.assertIn((2, 0), other)

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

    def test_v1_keepouts_bar_their_items_layers_and_nets_only(self):
        # A copper_keepout v1 (pnr.fixed_block) as rules.json carries it: its layers,
        # items and allow lists resolved to allowed_nets; a v0 one bars everything.
        from pnr.fanout.planner import _obstacles

        graph = board(array(3), {})
        comp = graph.component("U1")
        poly = [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]]
        r = rules()
        r["copper_keepouts"] = [
            dict(
                name="guard",
                polygon=poly,
                layers=["F.Cu", "In2.Cu"],
                items=["tracks"],
                allow_classes=["gnd"],
                allow_nets=[],
                allowed_nets=["GND"],
                exempt_groups=[],
            ),
            dict(name="vias", polygon=poly, layers=["In2.Cu"], items=["vias"], allowed_nets=[]),
            dict(name="old", ref="U1", rect_mm=[-1.0, -1.0, 1.0, 1.0]),
        ]
        layers = ["F.Cu", "In2.Cu", "B.Cu"]
        obs = _obstacles(graph, r, spec(), comp, Pose(comp.pos, comp.rot), layers, None)
        areas = {a[0]: a[2:] for a in obs.areas}
        self.assertEqual(areas["keepout:guard"], (frozenset({0, 1}), False, frozenset({"GND"})))
        self.assertEqual(areas["keepout:vias"], (frozenset(), True, frozenset()))
        self.assertEqual(areas["keepout:old"], (None, True, frozenset()))

    def test_a_fixed_via_of_its_net_on_the_site_is_reused_not_drilled_again(self):
        # GND vias of a fixed block on every interstitial site around the GND ball B2:
        # the drop joins one of them and the plan drills no second hole there.
        positions = array(3)
        nets = {n: "GND" if n == "B2" else "S_" + n for n in positions}
        graph = board(positions, nets)
        sites = [(20.0 + dx, 20.0 + dy) for dx in (-0.325, 0.325) for dy in (-0.325, 0.325)]
        fixed = dict(
            frame="engine-mm-y-up",
            tracks=[],
            vias=[dict(net="GND", xy=list(p), diameter_mm=0.35, drill_mm=0.15) for p in sites],
            polygons=[],
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
        row = p["terminals"]["B2"]
        self.assertEqual(row["kind"], "drop")
        self.assertTrue(row.get("via_existing"))
        self.assertTrue(any(math.dist(row["via"][:2], q) < 1e-6 for q in sites))
        for _net, x, y, _d, _h in p["copper"]["vias"]:
            self.assertTrue(all(math.dist((x, y), q) > 1e-6 for q in sites))
        self.assertEqual(p["diagnostics"]["vias_reused"], 1)

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

    def test_a_neck_keeps_a_current_class_minimum_unless_authorized(self):
        # neck_mm narrows a plain signal below the fab's default track width; a net of
        # a current-annotated class (width resolved from current_a) keeps its class
        # width unless the entry names the class in neck_classes. The validator's
        # required width (terminal_required_width) is the planned width.
        from pnr.pad_entry import terminal_required_width

        positions = array(5)
        nets = {n: "S_" + n if n[0] in "AE" else "GND" for n in positions}
        nets["A3"] = "S_PWR"
        graph = board(positions, nets)
        r = rules()
        r["fab"]["track_width_mm"] = 0.15
        r["net_classes"].append(
            dict(name="pwr", nets=["S_PWR"], width_mm=0.25, current_a=1.0, clearance_mm=0.1)
        )
        signals = {n.name for n in graph.nets if n.name != "GND"}
        for classes, pwr_width in (([], 0.25), (["pwr"], 0.1)):
            extra = {"neck_classes": classes} if classes else {}
            sp = spec(neck_mm=0.1, skip_pads=["E5"], **extra)
            p = plan(
                graph,
                r,
                sp,
                grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
                plane_nets={"GND"},
                signal_nets=signals,
            )
            t = p["terminals"]
            self.assertEqual(t["A1"]["width_mm"], 0.1)
            self.assertEqual(t["A3"]["width_mm"], pwr_width, classes)
            self.assertEqual(p["diagnostics"]["necks"]["A1"], [0.15, 0.1])
            self.assertEqual("A3" in p["diagnostics"]["necks"], bool(classes))
            for net, _la, _a, _b, w in p["copper"]["tracks"]:
                if net == "S_PWR":
                    self.assertEqual(w, pwr_width)
                elif net != "GND":
                    self.assertEqual(w, 0.1)
            rr = dict(r, fanouts=[sp])
            self.assertEqual(terminal_required_width("U1", "A1", "S_A1", rr), 0.1)
            self.assertEqual(terminal_required_width("U1", "A3", "S_PWR", rr), pwr_width)
            self.assertEqual(terminal_required_width("U1", "E5", "S_E5", rr), 0.15)  # skipped
            self.assertEqual(terminal_required_width("U1", "A1", "S_A1", rr, neck=False), 0.15)
            self.assertEqual(terminal_required_width("U2", "A1", "S_A1", rr), 0.15)
            self.assertEqual(terminal_required_width("U1", "B1", "GND", rr), 0.15)  # a plane

    def test_a_block_rule_area_bars_only_its_own_layers(self):
        # pnr.fixed_copper exports a block's rule area with its copper ``layers``; the
        # planner bars tracks on those layers only, as the router does.
        from pnr.fanout.planner import _obstacles

        graph = board(array(3), {})
        comp = graph.component("U1")
        poly = [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]]

        def area(layers, tracks, vias):
            return dict(kind="rule_area", layers=layers, outline=poly, tracks=tracks, vias=vias)

        fixed = dict(
            frame="engine-mm-y-up",
            blocks=[
                dict(
                    polygons=[
                        area(["F.Cu"], True, False),
                        area(["In2.Cu"], False, True),
                        area([], True, True),
                    ]
                )
            ],
        )
        layers = ["F.Cu", "In2.Cu", "B.Cu"]
        obs = _obstacles(graph, rules(), spec(), comp, Pose(comp.pos, comp.rot), layers, fixed)
        self.assertEqual(
            [a[2:4] for a in obs.areas], [(frozenset({0}), False), (frozenset(), True)]
        )

    def test_the_plan_keeps_the_oracles_micron(self):
        # The native Oracle judges every clearance with 1 um added: an inner track at
        # exactly the rule from a via is not planned.
        lat = infer(lands(array(3)))
        obs = Obstacles()
        obs.vias.append(("GND", (0.325, 0.325), 0.35, 0.15))
        m = Model(
            lat,
            ["F.Cu", "In2.Cu"],
            obs,
            clearance=0.1,
            via_to_pad=0.1,
            hole_to_hole=0.28,
            edge_clearance=0.3,
            hole_to_edge=0.5,
            via_classes=[DEFAULT],
            widths=[0.1],
        )
        # Along the row y = 0 on In2: 0.325 - 0.175 - 0.05 = 0.100 mm from the via.
        at_rule = m.segment_blockers(1, (-0.65, 0.0), (0.65, 0.0), 0.1)
        self.assertEqual(at_rule, frozenset({"GND"}))
        clear = m.segment_blockers(1, (-0.65, -0.01), (0.65, -0.01), 0.1)
        self.assertEqual(clear, frozenset())

    def test_drop_nets_take_a_via_instead_of_an_exit(self):
        # A supply decoupled under the array (or fed from an inner layer) needs only
        # a via beside each ball, even with every edge closed; no neck applies.
        from pnr.pad_entry import terminal_required_width

        positions = array(5)
        nets = {n: "GND" for n in positions}
        nets.update(A2="S_PA", B2="S_PA", C3="S_X")
        graph = board(positions, nets)
        edges = ["north", "south", "east", "west"]
        classes = dict(
            ground=dict(diameter_mm=0.35, drill_mm=0.15, nets=["GND"], sites=["interstitial"]),
            supply=dict(diameter_mm=0.35, drill_mm=0.15, nets=["S_P*"], sites=["interstitial"]),
            default=dict(diameter_mm=0.4, drill_mm=0.2, sites=["vacant", "outside"]),
        )
        sp = spec(drop_nets=["S_P*"], forbidden_exits=edges, neck_mm=0.1, via_classes=classes)
        self.assertEqual(sp["drop_nets"], ["S_P*"])
        sp = __import__("pnr.fanout", fromlist=["expand_nets"]).expand_nets(
            sp, [n.name for n in graph.nets]
        )
        self.assertEqual(sp["drop_nets"], ["S_PA"])
        r = rules()
        r["fab"]["track_width_mm"] = 0.15
        p = plan(
            graph,
            r,
            sp,
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets={"S_PA", "S_X"},
        )
        t = p["terminals"]
        for name in ("A2", "B2"):
            self.assertEqual(t[name]["kind"], "drop")
            self.assertEqual(t[name]["width_mm"], 0.15)
            self.assertNotIn("exit", t[name])
        self.assertEqual(t["C3"]["kind"], "failed")  # a signal still needs an exit
        self.assertNotIn("A2", p["diagnostics"].get("necks", {}))
        rr = dict(r, fanouts=[sp])
        self.assertEqual(terminal_required_width("U1", "A2", "S_PA", rr), 0.15)
        self.assertEqual(terminal_required_width("U1", "C3", "S_X", rr), 0.1)
        self.assertEqual(violations(p, graph), [])

    def test_best_round_legalizes_the_least_conflicted_round_too(self):
        # Every other ball of the outer four rings a signal, the rest ground: more than
        # the array can escape, and the negotiation's conflicts climb again after round
        # 7. PNR_FANOUT_BEST_ROUND legalizes that round as well and keeps the better.
        import os
        from unittest.mock import patch

        from pnr.fanout import planner

        positions = array(15, ufbga201)
        signals = {
            b
            for b in positions
            if min(ROWS.index(b[0]), int(b[1:]) - 1, 14 - ROWS.index(b[0]), 15 - int(b[1:])) < 4
            and (ROWS.index(b[0]) + int(b[1:]) - 1) % 2 == 0
        }
        graph = board(positions, self.nets(positions, signals))
        plans = {}
        for flag in ("0", "1"):
            planner._CACHE.clear()
            with patch.dict(os.environ, {"PNR_FANOUT_BEST_ROUND": flag}):
                plans[flag] = run(graph, spec())
        planner._CACHE.clear()
        off, on = plans["0"]["diagnostics"], plans["1"]["diagnostics"]
        self.assertNotIn("restored_round", off["search"])
        rounds = on["search"]["conflicts_per_round"]
        self.assertEqual(on["search"]["restored_round"], 1 + rounds.index(min(rounds)))
        self.assertEqual(on["search"]["conflicts_per_round"], off["search"]["conflicts_per_round"])
        self.assertGreater(on["signals_escaped"], off["signals_escaped"])
        self.assertGreaterEqual(on["drops_placed"], off["drops_placed"])
        self.assertEqual(violations(plans["1"], graph), [])

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


class FixedItemsTest(unittest.TestCase):
    def test_arcs_become_chords_within_a_micron(self):
        from pnr.fanout.planner import _arc_chords, fixed_items

        r = 1.403
        start, mid, end = (r, 0.0), (r / math.sqrt(2), r / math.sqrt(2)), (0.0, r)
        chords = _arc_chords("RF", "F.Cu", start, mid, end, 0.2)
        self.assertEqual(len(chords), 21)  # a 90 degree arc of R = 1.403 mm (design 1.4)
        for t in range(101):
            a = math.pi / 2 * t / 100
            p = (r * math.cos(a), r * math.sin(a))
            near = min(segment_segment(p, p, tuple(c[2]), tuple(c[3])) for c in chords)
            self.assertLessEqual(near, 0.001 + 1e-9)
        tracks, vias, polygons = fixed_items(
            dict(
                tracks=[],
                arcs=[["RF", "F.Cu", list(start), list(mid), list(end), 0.2]],
                blocks=[
                    dict(
                        tracks=[["GND", "F.Cu", [0, 0], [1, 0], 0.2]],
                        vias=[dict(net="GND", xy=[1, 1], diameter_mm=0.3, drill_mm=0.15)],
                        polygons=[
                            dict(
                                net="GND",
                                layer="In2.Cu",
                                outline=[[0, 0], [1, 0], [1, 1]],
                                kind="zone",
                            )
                        ],
                    )
                ],
            )
        )
        self.assertEqual(len(tracks), 22)
        self.assertEqual(vias, [("GND", [1, 1], 0.3, 0.15)])
        self.assertEqual(polygons[0]["kind"], "zone")


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
        # A fixed block's via where C1 went (a launch via under the array): C1 moves
        # off it, and placement's derive plans with the rules' fixed copper too.
        from pnr.fanout.bottom import derive
        from pnr.graph import footprint_point

        c1 = found["sites"]["C1"]
        graph.component("C1").pos, graph.component("C1").rot = tuple(c1["at"]), c1["rot"]
        graph.component("C1").side = "bottom"
        hit = footprint_point(graph.component("C1"), -0.48, 0.0)
        fixed = dict(
            frame="engine-mm-y-up",
            tracks=[],
            vias=[dict(net="X", xy=list(hit), diameter_mm=0.35, drill_mm=0.15)],
            polygons=[],
        )
        again = plan(
            graph,
            r,
            sp,
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND", "VCC"},
            signal_nets=set(),
            fixed_copper=fixed,
        )["bottom"]["sites"]
        self.assertNotEqual(again.get("C1", {}).get("at"), c1["at"])
        self.assertNotEqual(again.get("C2", {}).get("at"), c1["at"])
        from pnr.constraints import compile_constraints

        doc = {"schema": "v0", "board": {"outline": {"w": 40, "h": 40}}, "fixed": {}}
        cons = compile_constraints(doc, graph.refs)
        derived = derive(graph, cons, dict(r, fanouts=[sp], fixed_copper=fixed))
        posed = {c.refs[0]: c.params["at"] for c in derived.constraints if c.kind == "fixed"}
        self.assertNotEqual(posed.get("C1"), c1["at"])


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
        with self.assertRaisesRegex(FanoutError, "neck_mm.*min_track_width"):
            check(spec(neck_mm=0.08), rules(), ["F.Cu", "B.Cu"])
        self.assertEqual(check(spec(neck_mm=0.1), rules(), ["F.Cu", "B.Cu"]), [])
        warnings = check(spec(neck_mm=0.1, neck_classes=["nope"]), rules(), ["F.Cu", "B.Cu"])
        self.assertIn("neck_classes: nope is not a net class", warnings[0])
        with self.assertRaisesRegex(FanoutError, "neck_classes needs neck_mm"):
            spec(neck_classes=["gnd"])

    def test_the_fab_profile_adapts_an_unmade_via_class(self):
        from pnr import fab_profile as fp
        from pnr.fanout import check

        pofv = dict(rules(), fab=fp.apply_fab(FAB, "jlc-pofv"))
        # The ground class's 0.35/0.15 drop: jlc-pofv drills 0.20 at least; its filled
        # in-pad via (0.35/0.20) is no wider, so the class takes it and says so.
        sp = spec()
        warnings = check(sp, pofv, ["F.Cu", "In2.Cu", "B.Cu"])
        ground = next(c for c in sp["via_classes"] if c["name"] == "ground")
        self.assertEqual((ground["diameter_mm"], ground["drill_mm"]), (0.35, 0.2))
        self.assertEqual(ground["sites"], ["interstitial"])
        self.assertTrue(any("ground" in w and "filled in-pad via 0.35/0.2" in w for w in warnings))
        default = next(c for c in sp["via_classes"] if c["name"] == "default")
        self.assertEqual((default["diameter_mm"], default["drill_mm"]), (0.4, 0.2))  # made
        # Nothing to adapt to without filled vias (legacy, the design's own block).
        with self.assertRaisesRegex(FanoutError, "min_through_drill"):
            check(spec(), dict(rules(), fab=dict(FAB, min_through_drill_mm=0.2)), ["F.Cu"])
        # A declared via the fab makes is never touched.
        sp = spec(via_classes={"default": {"diameter_mm": 0.45, "drill_mm": 0.3}})
        self.assertEqual(check(sp, pofv, ["F.Cu"]), [])
        self.assertEqual(sp["via_classes"][0]["drill_mm"], 0.3)
        # Wider than the declared via: not a fit for its sites, so still an error.
        sp = spec(via_classes={"default": {"diameter_mm": 0.3, "drill_mm": 0.1}})
        with self.assertRaisesRegex(FanoutError, "min_through_drill"):
            check(sp, pofv, ["F.Cu"])

    def test_a_part_without_classes_escapes_by_the_profiles_filled_via(self):
        from pnr import fab_profile as fp
        from pnr.fanout import check

        sp = spec(via_classes={})
        sp["via_classes"] = []
        check(sp, dict(rules(), fab=fp.apply_fab(FAB, "jlc-pofv")), ["F.Cu"])
        (c,) = sp["via_classes"]
        self.assertEqual((c["diameter_mm"], c["drill_mm"]), (0.35, 0.2))
        self.assertEqual(c["sites"], ["in_pad", "interstitial", "vacant", "outside"])
        sp = spec(via_classes={})
        sp["via_classes"] = []
        check(sp, rules(), ["F.Cu"])  # no filled vias: the fab's default via, outside
        (c,) = sp["via_classes"]
        self.assertEqual((c["diameter_mm"], c["drill_mm"]), (0.4, 0.2))
        self.assertEqual(c["sites"], ["vacant", "outside"])

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

    def test_a_hierarchical_block_keeps_only_its_own_fanout(self):
        # pnr.hier: a block's sub-board compiles the authored constraints restricted
        # to its parts; a fanout of a part outside the block must not reach it.
        from pnr.constraints import compile_constraints
        from pnr.hier.blocks import block_constraints_doc

        doc = {
            "schema": "v0",
            "board": {"outline": {"w": 30, "h": 30}},
            "fanout": [dict(ref="U1", bottom_sites=dict(parts=["C1", "C2"]))],
        }
        mine = block_constraints_doc(doc, ["a.u1", "a.c1"], 10, 10, refs=["U1", "C1"])
        self.assertEqual(mine["fanout"], [dict(ref="U1", bottom_sites=dict(parts=["C1"]))])
        self.assertEqual(compile_constraints(mine, ["U1", "C1"]).fanouts[0]["ref"], "U1")
        other = block_constraints_doc(doc, ["b.r1"], 10, 10, refs=["R1"])
        self.assertNotIn("fanout", other)
        self.assertEqual(compile_constraints(other, ["R1"]).fanouts, [])
        self.assertNotIn("fanout", block_constraints_doc(doc, ["a.u1"], 10, 10))
        self.assertEqual(doc["fanout"][0]["bottom_sites"]["parts"], ["C1", "C2"])  # unchanged


if __name__ == "__main__":
    unittest.main()
