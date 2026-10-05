"""Coupled differential pairs inside route_board (``board.route_pairs: coupled``,
pnr.route.detail.pair_route): pads and fanout exits, exact gap and uncoupled length,
skew, layer restriction, fallback to legs and coupled group tuning."""

import math
import unittest

from pnr.constraints import ConstraintError, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.detail import coupled
from pnr.route.detail.regional import segment_distance
from pnr.route.detail.router import route_board

FAB = {
    "track_width_mm": 0.1,
    "clearance_mm": 0.1,
    "via_diameter_mm": 0.4,
    "via_drill_mm": 0.2,
    "hole_clearance_mm": 0.1524,
    "edge_clearance_mm": 0.3,
    "min_through_drill_mm": 0.15,
    "via_annular_mm": 0.0762,
    "smd_pad_clearance_mm": 0.1,
    "hole_to_hole_mm": 0.28,
    "via_to_smd_pad_mm": 0.1,
    "min_via_diameter_mm": 0.31,
}
STACK = {
    "layers": [
        {"name": "F.Cu", "type": "signal", "zones": []},
        {"name": "In1.Cu", "type": "power", "zones": ["GND"]},
        {"name": "In2.Cu", "type": "signal", "zones": []},
        {"name": "B.Cu", "type": "signal", "zones": []},
    ]
}
PITCH = 0.65


def _graph(name, comps, outline):
    pins = {}
    for c in comps:
        for p in c.pads:
            pins.setdefault(p.net, []).append((c.ref, p.name))
    nets = [Net(n, i + 1, p) for i, (n, p) in enumerate(sorted(pins.items()))]
    return BoardGraph(name, comps, nets, BoardOutline(*outline))


def _pad(name, net, at, size=(0.4, 0.3)):
    return Pad(name, net, at, size, land_corner=0.0)


def plain_board(second_pair=False):
    """U1 -> J1 across a 24 x 20 board: the pair DP/DN (0.65 mm pitch at both
    ends), a single net SIG, and optionally a second pair EP/EN whose ends are
    offset so that it runs longer."""
    u1 = [
        _pad("1", "DP", (0.6, 0.325)),
        _pad("2", "DN", (0.6, -0.325)),
        _pad("3", "SIG", (-0.6, 0)),
    ]
    j1 = [
        _pad("1", "DP", (-0.6, 0.325)),
        _pad("2", "DN", (-0.6, -0.325)),
        _pad("3", "SIG", (0.6, 0)),
    ]
    if second_pair:
        u1 += [_pad("4", "EP", (0.6, -1.675)), _pad("5", "EN", (0.6, -2.325))]
        j1 += [_pad("4", "EP", (-0.6, -7.675)), _pad("5", "EN", (-0.6, -8.325))]
    comps = [
        Component("U1", "u", (5.0, 10.0), 0.0, "top", (2.0, 6.0), (2.0, 6.0), True, u1),
        Component("J1", "j", (18.0, 11.0), 0.0, "top", (2.0, 18.0), (2.0, 18.0), True, j1),
    ]
    return _graph("pairs", comps, (24, 20))


def plain_rules(g, coupled_on=True, pair=None, extra=None):
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 24, "h": 20}, "layers": 2},
        "fixed": {
            "U1": {"at": [5, 10], "rot": 0, "side": "top"},
            "J1": {"at": [18, 11], "rot": 0, "side": "top"},
        },
        "diff_pair": [
            dict(
                {"name": "usb", "p": "DP", "n": "DN", "width_mm": 0.15, "gap_mm": 0.15},
                skew_mm=0.1,
                **(pair or {}),
            )
        ],
    }
    if coupled_on:
        doc["board"]["route_pairs"] = "coupled"
    doc.update(extra or {})
    c = compile_constraints(doc, g.refs)
    return c, compile_routing_rules(c, [n.name for n in g.nets])


def bga_board():
    """A 6 x 6 0.65 mm array at (10, 10): the outer two rings signals in a
    checkerboard (A4 a signal too, so A3/A4 are an adjacent pair), the rest GND.
    The pair goes to a two-pad connector north; every other signal to a pad on
    the south, west or east edge."""
    n = 6
    half = (n - 1) / 2
    pads, pins = [], {}
    for r in range(n):
        for c in range(n):
            name = "%s%d" % ("ABCDEF"[r], c + 1)
            ring = min(r, c, n - 1 - r, n - 1 - c)
            signal = ring < 2 and ((r + c) % 2 == 0 or name == "A4")
            net = ("S_" + name) if signal else "GND"
            at = ((c - half) * PITCH, (half - r) * PITCH)
            pads.append(Pad(name, net, at, (0.32, 0.32), land_corner=0.16))
            pins.setdefault(net, []).append(name)
    comps = [Component("U1", "bga", (10.0, 10.0), 0.0, "top", (5.0, 5.0), (5.0, 5.0), True, pads)]
    j1 = [Pad("1", "S_A3", (-0.4, 0), (0.3, 0.6), land_corner=0.0)]
    j1.append(Pad("2", "S_A4", (0.4, 0), (0.3, 0.6), land_corner=0.0))
    comps.append(Component("J1", "j", (14.0, 18.0), 0.0, "top", (3.0, 1.5), (3.0, 1.5), True, j1))
    others = sorted(net for net in pins if net.startswith("S_") and net not in ("S_A3", "S_A4"))
    for k, net in enumerate(others):
        t = 2.0 + 14.0 * (k // 3 + 0.5) / ((len(others) + 2) // 3)
        at = [(t, 1.0), (1.0, t), (19.0, t)][k % 3]
        pad = Pad("1", net, (0, 0), (0.3, 0.3), land_corner=0.15)
        comps.append(Component("T%d" % k, "t", at, 0.0, "top", (0.6, 0.6), (0.6, 0.6), pads=[pad]))
    gnd = Pad("1", "GND", (0, 0), (0.3, 0.3), land_corner=0.15)
    comps.append(Component("TG", "t", (19.0, 1.0), 0.0, "top", (0.6, 0.6), (0.6, 0.6), pads=[gnd]))
    g = _graph("fanout-pair", comps, (20, 20))
    g.stack = STACK
    return g


def bga_rules(g, coupled_on=True):
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 20, "h": 20}, "layers": 4},
        "fab": FAB,
        "fixed": {
            "U1": {"at": [10, 10], "rot": 0, "side": "top"},
            "J1": {"at": [14, 18], "rot": 0, "side": "top"},
        },
        "net_class": {"gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"}},
        "diff_pair": [
            {
                "name": "lvds",
                "p": "S_A3",
                "n": "S_A4",
                "width_mm": 0.1,
                "gap_mm": 0.15,
                "skew_mm": 0.1,
                "layers": ["F.Cu"],
                "max_uncoupled_mm": 3.0,
            }
        ],
        "fanout": [
            {
                "ref": "U1",
                "via_classes": {
                    "ground": {
                        "diameter_mm": 0.35,
                        "drill_mm": 0.15,
                        "nets": ["GND"],
                        "sites": ["interstitial"],
                    },
                    "default": {
                        "diameter_mm": 0.4,
                        "drill_mm": 0.2,
                        "sites": ["vacant", "outside", "interstitial"],
                    },
                },
            }
        ],
    }
    if coupled_on:
        doc["board"]["route_pairs"] = "coupled"
    c = compile_constraints(doc, g.refs)
    return c, compile_routing_rules(c, [n.name for n in g.nets])


def legs(route, *nets):
    return {
        net: [(t[1], tuple(t[2]), tuple(t[3]), t[4]) for t in route.tracks if t[0] == net]
        for net in nets
    }


def ordered_path(segments, start):
    """The leg's centre line from ``start`` as one ordered point list."""
    pending = [(a, b) for _la, a, b, _w in segments]
    path = [start]
    while pending:
        for k, (a, b) in enumerate(pending):
            if math.dist(a, path[-1]) < 1e-6:
                path.append(b)
                break
            if math.dist(b, path[-1]) < 1e-6:
                path.append(a)
                break
        else:
            raise AssertionError("leg is not one path from %r" % (start,))
        pending.pop(k)
    return path


def _project(p, n):
    """KiCad's commonParallelProjection: the parts of P and N over their common
    projection on P's line, or None."""
    (a, b), (c, d) = p, n
    length = math.dist(a, b)
    u = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
    t = sorted((q[0] - a[0]) * u[0] + (q[1] - a[1]) * u[1] for q in (c, d))
    if length <= t[0] or 0 >= t[1]:
        return None
    lo, hi = max(0.0, t[0]), min(length, t[1])
    clip_p = [(a[0] + u[0] * x, a[1] + u[1] * x) for x in (lo, hi)]

    def onto_n(q):
        dn = math.dist(c, d)
        v = ((d[0] - c[0]) / dn, (d[1] - c[1]) / dn)
        x = (q[0] - c[0]) * v[0] + (q[1] - c[1]) * v[1]
        return (c[0] + v[0] * x, c[1] + v[1] * x)

    return clip_p, [onto_n(q) for q in clip_p]


def _nearest(p, n):
    best = None
    for q, seg in [(q, n) for q in p] + [(q, p) for q in n]:
        (a, b) = seg
        ab = (b[0] - a[0], b[1] - a[1])
        ll = ab[0] ** 2 + ab[1] ** 2
        t = (
            0.0
            if ll < 1e-18
            else max(0.0, min(1.0, ((q[0] - a[0]) * ab[0] + (q[1] - a[1]) * ab[1]) / ll))
        )
        r = (a[0] + t * ab[0], a[1] + t * ab[1])
        pair = (q, r) if seg is n else (r, q)
        if best is None or math.dist(*pair) < math.dist(*best):
            best = pair
    return best


def kicad_gaps(route, p, n):
    """The edge gaps KiCad's diff_pair_gap rule judges (drc_test_provider_diff_pair_
    coupling.cpp): every P/N track pair on one layer whose lines are parallel and
    distinct and that shares a projection on P's line, at the nearest points of the shared parts,
    unless other copper on the layer lies on the line between those points."""
    tracks = [(t[0], t[1], tuple(t[2]), tuple(t[3]), t[4]) for t in route.tracks]
    pads = [(route.grid.layers[la], r) for la, _net, r in route.grid.pad_rectangles]
    gaps = []
    for sp in [t for t in tracks if t[0] == p]:
        for sn in [t for t in tracks if t[0] == n]:
            if sp[1] != sn[1] or math.dist(sp[2], sp[3]) < 1e-6 or math.dist(sn[2], sn[3]) < 1e-6:
                continue
            # Intersect(..., aLines=true): only parallel, non-collinear lines pass.
            u = (sp[3][0] - sp[2][0], sp[3][1] - sp[2][1])
            v = (sn[3][0] - sn[2][0], sn[3][1] - sn[2][1])
            if abs(u[0] * v[1] - u[1] * v[0]) > 1e-9 * math.hypot(*u) * math.hypot(*v):
                continue
            off = (sp[2][0] - sn[2][0], sp[2][1] - sn[2][1])
            if abs(off[0] * v[1] - off[1] * v[0]) < 1e-9 * math.hypot(*v):
                continue
            clipped = _project((sp[2], sp[3]), (sn[2], sn[3]))
            if clipped is None:
                continue
            near_p, near_n = _nearest(*clipped)
            ends = list(clipped[0]) + list(clipped[1])

            def hits(t, q):
                return segment_distance(t[2], t[3], q, q) <= t[4] / 2 + 1e-9

            def exits(r, t):
                inside = [
                    abs(q[0] - r.cx) <= r.w / 2 and abs(q[1] - r.cy) <= r.h / 2 for q in t[2:4]
                ]
                return inside[0] != inside[1]

            # excludeSelf: the two tracks, tracks directly connected to the coupled
            # parts' ends, and a pad either track exits.
            blocked = any(
                t is not sp
                and t is not sn
                and t[1] == sp[1]
                and not any(hits(t, q) for q in ends)
                and segment_distance(near_p, near_n, t[2], t[3]) < t[4] / 2
                for t in tracks
            ) or any(
                layer == sp[1]
                and not (exits(r, sp) or exits(r, sn))
                and segment_distance(near_p, near_n, (r.cx, r.cy), (r.cx, r.cy)) < min(r.w, r.h) / 2
                for layer, r in pads
            )
            if not blocked:
                gaps.append(math.dist(near_p, near_n) - (sp[4] + sn[4]) / 2)
    return gaps


def length_set(route, name):
    return next(s for s in route.length_report if s["name"] == name)


class Inputs(unittest.TestCase):
    def test_rules_keep_their_bytes_without_the_switch(self):
        g = plain_board()
        _c, rules = plain_rules(g, coupled_on=False)
        self.assertNotIn("route_pairs", rules)
        self.assertEqual(
            set(rules["diff_pairs"][0]), {"name", "p", "n", "width_mm", "gap_mm", "skew_mm"}
        )
        _c, rules = plain_rules(g, pair={"layers": ["F.Cu"], "max_uncoupled_mm": 1.5})
        self.assertEqual(rules["route_pairs"], "coupled")
        self.assertEqual(rules["diff_pairs"][0]["layers"], ["F.Cu"])
        self.assertEqual(rules["diff_pairs"][0]["max_uncoupled_mm"], 1.5)

    def test_bad_inputs_are_refused(self):
        g = plain_board()
        for pair in ({"layers": ["Top"]}, {"layers": []}, {"max_uncoupled_mm": 0}):
            with self.subTest(pair=pair), self.assertRaises(ConstraintError):
                plain_rules(g, pair=pair)
        with self.assertRaises(ConstraintError):
            plain_rules(g, coupled_on=False, extra={"board": {"route_pairs": "legs"}})


class PadsToPads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = plain_board()
        cls.c, cls.rules = plain_rules(cls.g)
        cls.route = route_board(cls.g, cls.c, cls.rules, pitch=0.25, max_iters=6)
        _c, rules = plain_rules(cls.g, coupled_on=False)
        cls.legs_route = route_board(cls.g, cls.c, rules, pitch=0.25, max_iters=6)

    def test_routed_coupled_and_everything_connects(self):
        r = self.route
        self.assertEqual(r.result.unrouted, [])
        row = r.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]
        self.assertEqual(row["status"], "coupled", row)
        self.assertEqual(row["layer"], "F.Cu")
        self.assertNotIn("coupled_pairs", self.legs_route.escape_diagnostics)

    def test_gap_is_exact_and_legs_never_closer(self):
        both = legs(self.route, "DP", "DN")
        pitch = 0.15 + 0.15
        closest = min(
            segment_distance(a, b, c, d)
            for _l, a, b, _w in both["DP"]
            for _m, c, d, _x in both["DN"]
        )
        self.assertAlmostEqual(closest, pitch, places=6)
        # Most of the P leg runs at exactly the pair pitch beside the N leg.
        parallel = 0.0
        for _l, a, b, _w in both["DP"]:
            for _m, c, d, _x in both["DN"]:
                if (
                    abs(segment_distance(a, a, c, d) - pitch) < 1e-6
                    and abs(segment_distance(b, b, c, d) - pitch) < 1e-6
                ):
                    parallel += math.dist(a, b)
                    break
        total = sum(math.dist(a, b) for _l, a, b, _w in both["DP"])
        self.assertGreater(parallel / total, 0.8)

    def test_uncoupled_and_skew_within_budget(self):
        both = legs(self.route, "DP", "DN")
        u1, j1 = self.g.component("U1"), self.g.component("J1")
        starts = {"DP": (5.6, 10.325), "DN": (5.6, 9.675)}
        steps = {
            net: [
                ("F.Cu", a, b)
                for a, b in zip(
                    ordered_path(both[net], starts[net]), ordered_path(both[net], starts[net])[1:]
                )
            ]
            for net in both
        }
        self.assertTrue(u1 and j1)
        runs = coupled.uncoupled_runs(steps, 0.15, 0.15)
        row = self.route.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]
        for net in ("DP", "DN"):
            total = sum(x["length_mm"] for x in runs[net]["runs"])
            self.assertLessEqual(total, 2.0 + 1e-6)
            self.assertAlmostEqual(total, row["uncoupled_mm"][net], places=6)
            # The leg ends exactly on its two pads.
            path = ordered_path(both[net], starts[net])
            self.assertLess(math.dist(path[-1], (17.4, 11.325 if net == "DP" else 10.675)), 1e-6)
        report = length_set(self.route, "usb")
        self.assertEqual(report["status"], "ok")
        self.assertLessEqual(report["spread"], 0.1)
        self.assertGreater(report["coupled_share"], 0.8)
        self.assertEqual(length_set(self.legs_route, "usb")["coupled_share"], 0.0)

    def test_other_copper_keeps_its_clearance(self):
        pair = [t for t in self.route.tracks if t[0] in ("DP", "DN")]
        other = [t for t in self.route.tracks if t[0] not in ("DP", "DN")]
        self.assertTrue(other)
        for t in pair:
            for o in other:
                if o[1] == t[1]:
                    gap = segment_distance(t[2], t[3], o[2], o[3]) - (t[4] + o[4]) / 2
                    self.assertGreaterEqual(gap, FAB["clearance_mm"] - 1e-6)


class Fallback(unittest.TestCase):
    def test_an_unsolvable_pair_routes_as_legs_on_its_layers(self):
        g = plain_board()
        c, rules = plain_rules(g, pair={"max_uncoupled_mm": 0.05, "layers": ["F.Cu"]})
        r = route_board(g, c, rules, pitch=0.25, max_iters=6)
        row = r.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]
        self.assertEqual(row["status"], "legs")
        self.assertEqual(row["reason"], "terminals_apart")
        self.assertEqual(row["terminal_distance_mm"], [0.65, 0.65])
        self.assertEqual(r.result.unrouted, [])
        self.assertEqual({t[1] for t in r.tracks if t[0] in ("DP", "DN")}, {"F.Cu"})

    def test_a_pair_without_a_common_layer_is_reported(self):
        g = plain_board()
        c, rules = plain_rules(g, pair={"layers": ["B.Cu"]})
        r = route_board(g, c, rules, pitch=0.25, max_iters=2)
        row = r.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]
        self.assertEqual((row["status"], row["reason"]), ("legs", "no_common_layer"))
        # The legs keep the declared layer too: SMD pads on F.Cu cannot be reached.
        self.assertEqual(sorted(set(r.result.unrouted)), ["DN", "DP"])


class FanoutExits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = bga_board()
        cls.c, cls.rules = bga_rules(cls.g)
        cls.route = route_board(cls.g, cls.c, cls.rules, pitch=0.25, max_iters=6)

    def test_pair_starts_at_its_fanout_exits(self):
        r = self.route
        self.assertEqual(r.result.unrouted, [])
        row = r.escape_diagnostics["coupled_pairs"]["pairs"]["lvds"]
        self.assertEqual(row["status"], "coupled", row)
        self.assertEqual(row["layer"], "F.Cu")
        self.assertIn(row["exit_room"], ("via", "track", "access"))
        # Both balls are surface escapes: the pair starts at the balls, so no
        # parallel stretch of its legs (escape stubs included) leaves the gap.
        self.assertEqual(row["start"], "balls")
        gaps = kicad_gaps(r, "S_A3", "S_A4")
        self.assertTrue(gaps)
        self.assertAlmostEqual(min(gaps), 0.15, places=6)
        self.assertLess(max(gaps), 0.15 + 0.01, sorted(gaps))
        tracks = legs(r, "S_A3", "S_A4")
        for net, ball in (("S_A3", (9.675, 11.625)), ("S_A4", (10.325, 11.625))):
            self.assertEqual({t[0] for t in tracks[net]}, {"F.Cu"})
            path = ordered_path(tracks[net], ball)  # escape then pair: one path
            pad = (13.6, 18.0) if net == "S_A3" else (14.4, 18.0)
            self.assertLess(math.dist(path[-1], pad), 1e-6)
            # The whole leg's uncoupled copper (escape lead included) is in budget.
            self.assertLessEqual(row["uncoupled_mm"][net], 3.0 + 1e-6)
        report = length_set(r, "lvds")
        self.assertLessEqual(report["spread"], 0.1)
        self.assertGreater(report["coupled_share"], 0.6)


class GroupTuning(unittest.TestCase):
    def test_coupled_pairs_of_a_group_are_matched_with_coupled_bumps(self):
        g = plain_board(second_pair=True)
        extra = {
            "diff_pair": [
                {
                    "name": "a",
                    "p": "DP",
                    "n": "DN",
                    "width_mm": 0.15,
                    "gap_mm": 0.15,
                    "skew_mm": 0.1,
                },
                {
                    "name": "b",
                    "p": "EP",
                    "n": "EN",
                    "width_mm": 0.15,
                    "gap_mm": 0.15,
                    "skew_mm": 0.1,
                },
            ],
            "length_match": [
                {"name": "bus", "nets": ["DP", "DN", "EP", "EN"], "tolerance_mm": 0.3}
            ],
        }
        c, rules = plain_rules(g, extra=extra)
        r = route_board(g, c, rules, pitch=0.25, max_iters=6)
        report = r.escape_diagnostics["coupled_pairs"]
        self.assertEqual({row["status"] for row in report["pairs"].values()}, {"coupled"})
        (group,) = report["groups"]
        self.assertGreater(group["spread_before_mm"], 0.3)
        self.assertLessEqual(group["spread_mm"], 0.3)
        self.assertEqual(group["status"], "tuned")
        self.assertGreater(sum(group["bumps"].values()), 0)
        # The KiCad-model measure agrees: group and both pairs within budget.
        self.assertLessEqual(length_set(r, "bus")["spread"], 0.3)
        for name in ("a", "b"):
            self.assertLessEqual(length_set(r, name)["spread"], 0.1)
        # Bumped legs stay at the pair pitch, never closer.
        for p, n in (("DP", "DN"), ("EP", "EN")):
            both = legs(r, p, n)
            closest = min(
                segment_distance(a, b, c2, d)
                for _l, a, b, _w in both[p]
                for _m, c2, d, _x in both[n]
            )
            self.assertGreaterEqual(closest, 0.3 - 1e-6)


if __name__ == "__main__":
    unittest.main()
