"""Partial fanouts (the fanout entry's ``partial``): a ball whose escape fails no longer
blocks its net (pnr.route.detail.fanout, route_board)."""

import math
import unittest
import unittest.mock

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.fanout.spec import FanoutError, parse
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.detail.router import route_board

PITCH = 0.65
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
N = 6
HALF = (N - 1) / 2


def ball_xy(name):
    """A ball's board centre (U1 at (10, 10), rot 0)."""
    r, c = "ABCDEFGH".index(name[0]), int(name[1:]) - 1
    return (10.0 + (c - HALF) * PITCH, 10.0 + (HALF - r) * PITCH)


def board(extra_terminal=True):
    """The 6 x 6 0.65 mm array of test_fanout_route: the outer two rings' checkerboard
    balls are signals S_*, the rest GND (In1 plane). Each signal has a target pad on an
    edge; S_A1 has a second one (three terminals), so it can route without its ball."""
    pads, pins = [], {}
    for r in range(N):
        for c in range(N):
            name = "%s%d" % ("ABCDEFGH"[r], c + 1)
            ring = min(r, c, N - 1 - r, N - 1 - c)
            net = ("S_" + name) if ring < 2 and (r + c) % 2 == 0 else "GND"
            pads.append(
                Pad(
                    name,
                    net,
                    ((c - HALF) * PITCH, (HALF - r) * PITCH),
                    (0.32, 0.32),
                    land_corner=0.16,
                )
            )
            pins.setdefault(net, []).append(("U1", name))
    comps = [Component("U1", "bga", (10.0, 10.0), 0.0, "top", (5.0, 5.0), (5.0, 5.0), True, pads)]
    signals = sorted(n for n in pins if n.startswith("S_"))
    targets = [(net, k) for k, net in enumerate(signals)]
    if extra_terminal:
        targets.append(("S_A1", len(signals)))
    count = len(targets)
    for net, k in targets:
        side = k % 4
        t = 2.0 + 16.0 * (k // 4 + 0.5) / ((count + 3) // 4)
        at = [(t, 1.0), (t, 19.0), (1.0, t), (19.0, t)][side]
        ref = "T%d" % k
        comps.append(
            Component(
                ref,
                "t",
                at,
                0.0,
                "top",
                (0.6, 0.6),
                (0.6, 0.6),
                pads=[Pad("1", net, (0, 0), (0.3, 0.3), land_corner=0.15)],
            )
        )
        pins[net].append((ref, "1"))
    comps.append(
        Component(
            "TG",
            "t",
            (19.0, 1.0),
            0.0,
            "top",
            (0.6, 0.6),
            (0.6, 0.6),
            pads=[Pad("1", "GND", (0, 0), (0.3, 0.3), land_corner=0.15)],
        )
    )
    pins["GND"].append(("TG", "1"))
    nets = [Net(name, i + 1, p) for i, (name, p) in enumerate(sorted(pins.items()))]
    g = BoardGraph("fanout-partial", comps, nets, BoardOutline(20, 20))
    g.stack = STACK
    return g


def setup(partial=None, reserved=(), keepouts=(), **extra):
    g = board()
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 20, "h": 20}, "layers": 4},
        "fab": FAB,
        "fixed": {"U1": {"at": [10, 10], "rot": 0, "side": "top"}},
        "net_class": {"gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"}},
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
                **({"partial": partial} if partial is not None else {}),
                **({"reserved": list(reserved)} if reserved else {}),
                **extra,
            }
        ],
    }
    if keepouts:
        doc["copper_keepout"] = list(keepouts)
    c = compile_constraints(doc, g.refs)
    return g, c, compile_routing_rules(c, [n.name for n in g.nets])


def around(name, half):
    """A part-frame square of half-size ``half`` around ball ``name``."""
    x, y = ball_xy(name)
    x, y = x - 10.0, y - 10.0
    return [[x - half, y - half], [x + half, y - half], [x + half, y + half], [x - half, y + half]]


def route(*args, **kwargs):
    g, c, rules = setup(*args, **kwargs)
    return rules, route_board(g, c, rules, pitch=0.25, max_iters=6)


class PartialSpecTest(unittest.TestCase):
    def test_parse(self):
        base = dict(ref="U1")
        self.assertNotIn("partial", parse(base, ["U1"]))
        self.assertNotIn("partial", parse(dict(base, partial=False), ["U1"]))
        self.assertEqual(parse(dict(base, partial=True), ["U1"])["partial"], {"bridge": False})
        self.assertEqual(
            parse(dict(base, partial={"bridge": True}), ["U1"])["partial"], {"bridge": True}
        )
        for bad in ("yes", {"bridge": 1}, {"bridges": True}):
            with self.assertRaises(FanoutError):
                parse(dict(base, partial=bad), ["U1"])


# Every fanout site of S_A1's ball removed (the corner ball's exits and dog-bone sites).
A1_RESERVED = dict(name="a1", polygon=around("A1", 0.3), layers=["*"])
# And a copper keepout over it on every layer, so the board's escapes fail too.
A1_KEEPOUT = dict(
    name="a1",
    rect=[
        ball_xy("A1")[0] - 0.3,
        ball_xy("A1")[1] - 0.3,
        ball_xy("A1")[0] + 0.3,
        ball_xy("A1")[1] + 0.3,
    ],
    layers=["F.Cu", "In2.Cu", "B.Cu"],
    items=["tracks", "vias"],
)


class PartialRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.off = route(reserved=[A1_RESERVED], keepouts=[A1_KEEPOUT])[1]
        cls.on = route(partial=True, reserved=[A1_RESERVED], keepouts=[A1_KEEPOUT])[1]

    def test_off_a_failed_ball_blocks_its_net(self):
        self.assertIn("S_A1", self.off.result.unrouted)
        self.assertEqual(self.off.partial_open, {})
        self.assertNotIn("partial", self.off.escape_diagnostics["fanout"]["U1"])

    def test_on_the_net_routes_among_its_other_terminals(self):
        r = self.on
        self.assertNotIn("S_A1", r.result.unrouted)
        self.assertEqual(list(r.partial_open), ["S_A1"])
        self.assertEqual(list(r.partial_open["S_A1"]), ["U1.A1"])
        self.assertTrue(r.partial_open["S_A1"]["U1.A1"].startswith("plan: "))
        self.assertFalse(r.fully_routed)
        report = r.escape_diagnostics["fanout"]["U1"]["partial"]
        self.assertEqual(report["A1"]["outcome"], "open")
        self.assertEqual(report["A1"]["net"], "S_A1")
        # The ball is localized as a failure site, and its two targets are joined.
        site = ball_xy("A1")
        self.assertTrue(any(math.dist(site, p) < 1e-6 for p in r.failure_sites["S_A1"]))
        self.assertTrue([t for t in r.tracks if t[0] == "S_A1"])
        # Nothing else changes: the other signals are routed as without the flag.
        self.assertEqual(
            sorted(set(r.result.unrouted)), sorted(set(self.off.result.unrouted) - {"S_A1"})
        )

    def test_a_two_terminal_net_stays_blocked(self):
        # S_A3 has its ball and one target: without the ball there is nothing to join.
        reserved = dict(name="a3", polygon=around("A3", 0.3), layers=["*"])
        x, y = ball_xy("A3")
        keepout = dict(
            name="a3",
            rect=[x - 0.3, y - 0.3, x + 0.3, y + 0.3],
            layers=["F.Cu", "In2.Cu", "B.Cu"],
            items=["tracks", "vias"],
        )
        r = route(partial=True, reserved=[reserved], keepouts=[keepout])[1]
        self.assertIn("S_A3", r.result.unrouted)
        self.assertIn("U1.A3", r.partial_open["S_A3"])


class PartialRetryTest(unittest.TestCase):
    def test_the_boards_escapes_get_a_second_try(self):
        # Only the fanout's own sites are gone: the board's escape planner escapes the
        # corner ball, so nothing is open.
        # (Whether the maze then reaches that escape is the maze's business.)
        rules, r = route(partial=True, reserved=[A1_RESERVED])
        self.assertEqual(r.partial_open, {})
        report = r.escape_diagnostics["fanout"]["U1"]["partial"]
        self.assertEqual(report["A1"]["outcome"], "escaped by the board's escapes")


class PartialBridgeTest(unittest.TestCase):
    """C3 is a GND ball. Its plan is made to fail (as a ball without a drop site
    does); the bridge joins it to an adjacent GND ball whose drop stands."""

    @staticmethod
    def failing(ball):
        import copy

        import pnr.fanout as fanout

        real = fanout.cached_plan

        def plan(*args, **kwargs):
            out = copy.deepcopy(real(*args, **kwargs))
            row = out["terminals"][ball]
            out["terminals"][ball] = dict(
                net=row["net"],
                kind="failed",
                intended="drop",
                reason="no via site (test)",
                ring=row["ring"],
                width_mm=row["width_mm"],
                pad_xy=row["pad_xy"],
            )
            return out

        return unittest.mock.patch.object(fanout, "cached_plan", plan)

    def test_a_failed_plane_ball_is_bridged_to_its_neighbour(self):
        with self.failing("C3"):
            off = route(partial=True)[1]
            on = route(partial={"bridge": True})[1]
            blocked = route()[1]
        self.assertIn("GND", blocked.result.unrouted)  # without partial: blocked
        report = off.escape_diagnostics["fanout"]["U1"]["partial"]
        self.assertFalse(report["C3"]["outcome"].startswith("bridged"))
        bridged = on.escape_diagnostics["fanout"]["U1"]["partial"]["C3"]["outcome"]
        self.assertTrue(bridged.startswith("bridged to "), bridged)
        other = bridged.split()[-1]
        a, b = ball_xy("C3"), ball_xy(other)
        self.assertAlmostEqual(math.dist(a, b), PITCH, places=6)

        def same(t):
            ends = {tuple(round(v, 6) for v in t[2]), tuple(round(v, 6) for v in t[3])}
            return ends == {tuple(round(v, 6) for v in a), tuple(round(v, 6) for v in b)}

        stubs = [t for t in on.tracks if t[0] == "GND" and t[1] == "F.Cu" and same(t)]
        self.assertEqual(len(stubs), 1)
        self.assertNotIn("GND", on.result.unrouted)
        self.assertEqual(on.partial_open, {})
        # The stub is locked fanout copper.
        locked = [t for t in on.locked["tracks"] if t[0] == "GND" and same([0, 0] + t[2:])]
        self.assertEqual(len(locked), 1)


if __name__ == "__main__":
    unittest.main()
