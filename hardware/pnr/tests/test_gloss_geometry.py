"""Pure-geometry tests for pnr.gloss_geometry (PNR_GLOSS core; no pcbnew).

Synthetic geometry with an injected Oracle-like `clear` (edge gap >= max clearance
+ 0.001 mm, as pnr.native_electrical.Oracle.clear). The router-shaped cases (a long
staircase chain, a U detour, a duplicate segment carrying a joint) are generated
here, not taken from any board.
"""

import math
import os
import random
import unittest

from pnr import gloss_geometry as g
from pnr.route.detail import keyhole

MM = 1_000_000
W = 200_000  # Default class width (nm)
C = 127_000  # a fine-pitch fab clearance, e.g. the jlc-pofv profile (nm)


def mm(x, y):
    return (round(x * MM), round(y * MM))


def seg(i, a, b, w=W, **kw):
    return g.Seg(str(i), tuple(a), tuple(b), w, **kw)


def polyline_segs(points, prefix="s", w=W):
    return [
        seg("%s%03d" % (prefix, k), a, b, w) for k, (a, b) in enumerate(zip(points, points[1:]))
    ]


class World:
    """Foreign copper for an injected clear(): discs (vias/pads) and capsules (tracks)."""

    def __init__(self, clearance=C):
        self.clearance = clearance
        self.discs = []  # (net, centre, radius)
        self.caps = []  # (net, a, b, half_width)

    def via(self, net, p, r=225_000):
        self.discs.append((net, p, r))
        return self

    def track(self, net, a, b, w=W):
        self.caps.append((net, a, b, w / 2))
        return self

    def clear_for(self, net, width=W):
        need = width / 2 + self.clearance + g.MARGIN

        def clear(a, b):
            for n, c, r in self.discs:
                if n != net and g.point_segment_distance(c, a, b) < need + r - 1e-6:
                    return False
            for n, p, q, hw in self.caps:
                if n != net and g.segment_distance(a, b, p, q) < need + hw - 1e-6:
                    return False
            return True

        return clear

    def points(self, net=None):
        pts = [c for n, c, _ in self.discs if n != net]
        for n, p, q, _ in self.caps:
            if n != net:
                pts += [p, q]
        return pts

    def ctx(self, net, width=W, **kw):
        return g.Context(width, clear=self.clear_for(net, width), obstacles=self.points(net), **kw)


def staircase(origin, steps, a=(250_000, 0), b=(250_000, -250_000)):
    pts = [origin]
    for k in range(steps):
        d = a if k % 2 == 0 else b
        pts.append((pts[-1][0] + d[0], pts[-1][1] + d[1]))
    return pts


# Generated geometry (nm, octilinear on a 0.25 mm lattice; no board data). D: unit steps of the
# eight directions, y down as in KiCad.
D = dict(E=(1, 0), NE=(1, -1), N=(0, -1), NW=(-1, -1), W=(-1, 0), SW=(-1, 1), S=(0, 1), SE=(1, 1))


def route(legs, origin, unit=250_000):
    """Polyline from (direction, steps) legs."""
    pts = [origin]
    for d, n in legs:
        pts.append((pts[-1][0] + D[d][0] * n * unit, pts[-1][1] + D[d][1] * n * unit))
    return pts


def staircase_route(origin=(10 * MM, 30 * MM)):
    """A router-style signal chain between two vias, as normalize leaves it: 42 legs, 41 bends.
    45 deg staircases in four cones ({S, SE, E}, {E, NE}, {N, NE}, {N, NW}), a long diagonal, a
    long vertical with a small jog and a short end hook."""
    legs = [("S", 2), ("SE", 1), ("E", 2), ("SE", 1), ("E", 1)]
    legs += [
        ("NE", (1, 2)[k % 4 // 2]) if k % 2 == 0 else ("E", (1, 2, 4)[k % 3]) for k in range(22)
    ]
    legs += [("NE", 14)]
    legs += [("N", (4, 8, 12)[k // 2]) if k % 2 == 0 else ("NE", 1) for k in range(6)]
    legs += [("NW", 3), ("N", 50), ("NE", 1), ("N", 1), ("NW", 1), ("N", 1), ("W", 3), ("SW", 1)]
    return route(legs, origin)


def u_detour(origin=(10 * MM, 15 * MM)):
    """Pad to pad with a U overshoot: out west, up past the target, across, back down, east."""
    legs = [("W", 4), ("NW", 4), ("N", 28), ("NE", 4), ("E", 14), ("S", 16), ("E", 21), ("NE", 3)]
    return route(legs, origin, unit=125_000)


STAIRS = staircase_route()
UTURN = u_detour()
# A trunk, a short duplicate lying on its start, a stub hanging off the duplicate's far end (a T
# point on the trunk's interior) and a tail from the stub to a zero-length dot at (5, 19).
FAULT = [
    ("trunk", mm(10, 20), mm(15, 15)),
    ("dup", mm(10, 20), mm(10.5, 19.5)),
    ("stub", mm(10.5, 19.5), mm(10.5, 19)),
    ("tail", mm(5, 19), mm(10.5, 19)),
]


def shift(points, dx, dy):
    return [(p[0] + dx, p[1] + dy) for p in points]


def covered(segs, p, slack=0):
    return any(g.point_segment_distance(p, s.a, s.b) <= s.width / 2 + slack for s in segs)


# ======================================================================= primitives and rule R
class PrimitiveTest(unittest.TestCase):
    def test_core_has_no_pcbnew_dependency(self):
        import subprocess
        import sys

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                'import sys, pnr.gloss_geometry; print("pcbnew" in sys.modules)',
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p)),
        )
        self.assertEqual(out.stdout.strip(), "False")

    def test_direction_and_turn_classes(self):
        self.assertEqual(
            [g.direction((0, 0), (d[0] * 1000, d[1] * 1000)) for d in g.DIRS], list(range(8))
        )
        self.assertEqual(
            [g.vector_turn(g.DIRS[0], d) for d in g.DIRS], [0, 45, 90, 135, 180, 135, 90, 45]
        )
        self.assertIsNone(g.direction((0, 0), (10, 3)))
        self.assertEqual(g.direction((0, 0), (1000, 1001)), 1)
        pts = [
            (0, 0),
            (10, 0),
            (20, 10),
            (20, 20),
            (10, 30),
            (10, 20),
            (10, 40),
            (0, 40),
            (-10, 37),
        ]
        self.assertEqual(g.bend_histogram(pts), {45: 3, 90: 1, 135: 1, 180: 1, "other": 1})
        self.assertEqual(g.bends(pts), 7)
        self.assertEqual(g.sharp_turns(pts), 3)

    def test_simplify_merges_collinear_only(self):
        pts = [(0, 0), (5, 0), (10, 0), (10, 0), (10, 5), (10, 0)]
        self.assertEqual(g.simplify(pts), [(0, 0), (10, 0), (10, 5), (10, 0)])

    def test_rule_r(self):
        old = [(0, 0), (0, 1000), (1000, 1000)]
        self.assertTrue(g.rule_r(old, [(0, 0), (1000, 1000)]))
        longer = [(0, 0), (0, 2000), (1000, 2000), (1000, 1000)]
        self.assertFalse(g.rule_r(old, longer))
        self.assertFalse(g.rule_r(old, old))
        # equal length, fewer bends
        stair = staircase((0, 0), 6)
        corner = [stair[0], (stair[-1][0] - (stair[0][1] - stair[-1][1]), stair[0][1]), stair[-1]]
        self.assertAlmostEqual(g.length(stair), g.length(corner), delta=1)
        self.assertTrue(g.rule_r(stair, corner))
        # shorter but more bends and more sharp turns: rejected
        z = [(0, 0), (10 * MM, 0)]
        self.assertFalse(g.rule_r(z, [(0, 0), (5 * MM, 0), (5 * MM, 1), (10 * MM, 1)]))

    def test_rule_r_never_accepts_longer_randomised(self):
        rng = random.Random(7)
        for _ in range(300):
            old = [(0, 0)]
            for _ in range(rng.randint(1, 6)):
                d = g.DIRS[rng.randrange(8)]
                n = rng.randint(1, 8) * 125_000
                old.append((old[-1][0] + d[0] * n, old[-1][1] + d[1] * n))
            new = [old[0]]
            for _ in range(rng.randint(0, 5)):
                d = g.DIRS[rng.randrange(8)]
                n = rng.randint(1, 8) * 125_000
                new.append((new[-1][0] + d[0] * n, new[-1][1] + d[1] * n))
            new.append(old[-1])
            if g.rule_r(old, new):
                self.assertLessEqual(g.length(new), g.length(old) + g.LENGTH_TOL)
                self.assertLess(g.cost(new), g.cost(old))

    def test_winding_and_swept(self):
        old = [(0, 0), (0, 10), (10, 10)]
        new = [(0, 0), (10, 0), (10, 10)]
        self.assertEqual(g.swept_points(old, new, [(5, 5), (20, 20), (-5, 5)]), [(5, 5)])
        self.assertEqual(g.swept_points(old, old, [(5, 5)]), [])
        self.assertAlmostEqual(g.swept_area(old, new), 100)
        # self-crossing replacement: |w|-weighted area of both lobes
        old = [(0, 0), (0, 10), (20, 10), (20, 0)]
        new = [(0, 0), (20, 10), (20, 0)]
        self.assertAlmostEqual(g.swept_area(old, new), 100)

    def test_pitch(self):
        self.assertEqual(g.pitch(W, C, W, C), 328_000)
        self.assertEqual(g.pitch(W, C, 500_000, 150_000), (W + 500_000) / 2 + 150_000 + 1_000)

    def test_eligibility_and_class(self):
        self.assertEqual(g.eligibility("a", mode="signal")[0], True)
        self.assertEqual(g.eligibility("a", mode="power")[2], "E1:power")
        self.assertEqual(g.eligibility("a", mode="pair")[2], "E1:pair")
        self.assertEqual(g.eligibility("a", mode="signal", protected={"a"})[2], "E2:protected")
        self.assertEqual(g.eligibility("a", mode="signal", si_nets={"a"})[2], "E3:si")
        self.assertEqual(
            g.eligibility("a", mode="signal", si_nets={"a"}, gloss_si=True), (True, True, None)
        )
        self.assertEqual(g.eligibility("a", mode="signal", netclass="bus")[2], "E4:netclass")
        self.assertEqual(
            g.eligibility("a", mode="signal", arc_layers={"F.Cu"}, layer="F.Cu")[2], "E5:arc"
        )
        rules = dict(
            fab=dict(track_width_mm=0.2, clearance_mm=0.127),
            net_classes=[dict(name="logic", nets=["vlog"], width_mm=0.5)],
            diff_pairs=[dict(p="Dp", n="Dn", gap_mm=0.15)],
        )
        self.assertTrue(g.net_record("bus_d", rules)["eligible"])
        self.assertEqual(g.net_record("bus_d", rules)["clearance"], C)
        self.assertFalse(g.net_record("vlog", rules)["eligible"])
        self.assertFalse(g.net_record("Dp", rules)["eligible"])

        def rec(net, key, si=False, ok=True):
            return dict(net=net, key=key, si=si, eligible=ok)

        k = g.class_key("In2.Cu", "Default", W, C)
        self.assertTrue(g.groupable(rec("a", k), rec("b", k)))
        self.assertFalse(g.groupable(rec("a", k), rec("a", k)))
        self.assertFalse(g.groupable(rec("a", k), rec("b", k, si=True)))
        self.assertFalse(
            g.groupable(rec("a", k), rec("b", g.class_key("In2.Cu", "Default", 500_000, C)))
        )
        self.assertFalse(
            g.groupable(rec("a", k), rec("b", g.class_key("In2.Cu", "Default", W, 150_000)))
        )


# ======================================================================= chains and anchors
class ChainTest(unittest.TestCase):
    def test_t_junction_split_and_degree3(self):
        segs = [seg("a", (0, 0), (10 * MM, 0)), seg("b", (4 * MM, 0), (4 * MM, 5 * MM))]
        cs = g.build_chains(segs, net="n", layer="L")
        self.assertIn((4 * MM, 0), cs.anchors)
        self.assertEqual(
            sorted((c.points[0], c.points[-1]) for c in cs.chains),
            [((0, 0), (4 * MM, 0)), ((4 * MM, 0), (4 * MM, 5 * MM)), ((4 * MM, 0), (10 * MM, 0))],
        )

    def test_pad_via_width_and_locked_anchors(self):
        pts = [(0, 0), (MM, 0), (2 * MM, 0), (3 * MM, MM), (4 * MM, MM), (5 * MM, MM), (6 * MM, MM)]
        segs = polyline_segs(pts)
        segs[4] = g.Seg(segs[4].id, segs[4].a, segs[4].b, 300_000)  # width change at (4,1)/(5,1)
        segs[5] = g.Seg(segs[5].id, segs[5].a, segs[5].b, 300_000, locked=True)

        def pinned(p):  # via pad at (1, 0)
            return p == (MM, 0)

        cs = g.build_chains(segs, pinned=pinned, pin_points=[(2 * MM, 0)])  # pad-centre projection
        ends = sorted((c.points[0], c.points[-1], c.width, c.frozen) for c in cs.chains)
        self.assertEqual(
            ends,
            [
                ((0, 0), (MM, 0), W, False),
                ((MM, 0), (2 * MM, 0), W, False),
                ((2 * MM, 0), (4 * MM, MM), W, False),
                ((4 * MM, MM), (5 * MM, MM), 300_000, False),
                ((5 * MM, MM), (6 * MM, MM), 300_000, True),
            ],
        )
        cs = g.build_chains(segs, pinned=pinned, pin_points=[(2 * MM, 0)], class_width=W)
        self.assertEqual(cs.off_width, 2)

    def test_anchor_free_loop_and_closed_chain_skipped(self):
        square = [(0, 0), (MM, 0), (MM, MM), (0, MM), (0, 0)]
        cs = g.build_chains(polyline_segs(square))
        self.assertEqual(cs.chains, [])
        self.assertEqual(cs.loops, 4)
        cs = g.build_chains(polyline_segs(square), pin_points=[(0, 0)])
        self.assertEqual((cs.chains, cs.closed), ([], 1))

    def test_fault_duplicate_and_zero_length(self):
        segs = [seg(i, a, b) for i, a, b in FAULT] + [seg("dot", mm(5, 19), mm(5, 19))]
        cs = g.build_chains(segs)
        # the duplicate and the trunk piece form a 2-cycle closing on the T point: skipped
        self.assertIn(mm(10.5, 19.5), cs.anchors)
        self.assertIn(mm(5, 19), cs.anchors)  # zero-length dot is an anchor
        self.assertEqual(cs.closed, 1)
        self.assertFalse(any("dup" in c.seg_ids for c in cs.chains))
        norm = g.normalize(segs)
        # The duplicate carries the stub's joint: without it the stub would end on the trunk's
        # interior and the trunk's start would dangle (KiCad reports track_dangling). It is
        # kept; only the zero-length dot goes.
        self.assertEqual(norm.delete, ["dot"])
        self.assertEqual(norm.counts, dict(zero=1, duplicate=0, merged=0, duplicate_joint=1))
        # The same duplicate without the stub (no joint at its far end) is removed.
        alone = [s for s in segs if s.id != "stub"]
        norm = g.normalize(alone)
        self.assertEqual(norm.delete, ["dot", "dup"])
        self.assertEqual(norm.counts, dict(zero=1, duplicate=1, merged=0, duplicate_joint=0))
        after = g.build_chains(norm.segments)
        self.assertEqual(after.closed, 0)

    def test_shuffled_input_identical_chains(self):
        pts = staircase((0, 0), 12) + [(10 * MM, -10 * MM)]
        segs = polyline_segs(pts) + [seg("br", pts[5], (pts[5][0], pts[5][1] + 3 * MM))]
        ref = [(c.points, c.pieces) for c in g.build_chains(segs).chains]
        rng = random.Random(3)
        for _ in range(10):
            shuffled = list(segs)
            rng.shuffle(shuffled)
            shuffled = [
                s if rng.random() < 0.5 else g.Seg(s.id, s.b, s.a, s.width) for s in shuffled
            ]
            got = [(c.points, sorted(c.seg_ids)) for c in g.build_chains(shuffled).chains]
            self.assertEqual([(p, sorted({x[2] for x in q})) for p, q in ref], got)


# ======================================================================= normalize
class NormalizeTest(unittest.TestCase):
    def assert_same_copper(self, before, after):
        box = g.bbox([p for s in before for p in (s.a, s.b)], W)
        for x in range(box[0], box[2], 37_000):
            for y in range(box[1], box[3], 37_000):
                p = (x, y)
                if covered(before, p):
                    self.assertTrue(covered(after, p, 2), p)
                if covered(after, p):
                    self.assertTrue(covered(before, p, 2), p)

    def test_union_identical_survivor_uuid_and_t_point(self):
        pts = [(0, 0), (MM, 0), (2 * MM, 0), (3 * MM, 0), (3 * MM, MM), (3 * MM, 2 * MM)]
        segs = polyline_segs(pts, "z")
        segs.append(seg("t", (MM, 0), (MM, -MM)))  # T at (1, 0): no merge there
        segs.append(seg("dup", (2 * MM + 100_000, 0), (2 * MM + 600_000, 0)))  # duplicate on z002
        segs.append(seg("dot", (3 * MM, MM), (3 * MM, MM)))
        norm = g.normalize(segs)
        self.assert_same_copper(segs, norm.segments)
        self.assertIn("dup", norm.delete)
        self.assertIn("dot", norm.delete)
        by = {s.id: s for s in norm.segments}
        # (1,0) carries the T stub: z000 and z001 stay separate; z001+z002 merge keeping z001
        self.assertEqual((by["z000"].a, by["z000"].b), ((0, 0), (MM, 0)))
        self.assertEqual({by["z001"].a, by["z001"].b}, {(MM, 0), (3 * MM, 0)})
        self.assertEqual({by["z003"].a, by["z003"].b}, {(3 * MM, 0), (3 * MM, 2 * MM)})
        self.assertNotIn("z002", by)
        self.assertNotIn("z004", by)
        self.assertEqual(norm.modify["z001"], (by["z001"].a, by["z001"].b))
        # the T stub still lands on copper of its own net
        self.assertTrue(covered(norm.segments, (MM, 0)))
        cs = g.build_chains(norm.segments)
        self.assertIn((MM, 0), cs.anchors)

    def test_no_merge_across_anchor_or_frozen(self):
        pts = [(0, 0), (MM, 0), (2 * MM, 0), (3 * MM, 0)]
        segs = polyline_segs(pts)
        self.assertEqual(g.normalize(segs, pinned=lambda p: p == (MM, 0)).counts["merged"], 1)
        self.assertEqual(g.normalize(segs, pin_points=[(MM, 0), (2 * MM, 0)]).counts["merged"], 0)
        frozen = [segs[0], g.Seg(segs[1].id, segs[1].a, segs[1].b, W, frozen=True), segs[2]]
        self.assertEqual(g.normalize(frozen).counts["merged"], 0)
        wide = [segs[0], g.Seg(segs[1].id, segs[1].a, segs[1].b, 300_000), segs[2]]
        self.assertEqual(
            g.normalize(wide).counts, dict(zero=0, duplicate=0, merged=0, duplicate_joint=0)
        )
        # a covered duplicate is removed even next to a frozen trunk; the frozen one never is
        dup = [g.Seg("trunk", (0, 0), (3 * MM, 0), W, locked=True), seg("d", (MM, 0), (2 * MM, 0))]
        self.assertEqual(g.normalize(dup).delete, ["d"])
        dup = [seg("trunk", (0, 0), (3 * MM, 0)), g.Seg("d", (MM, 0), (2 * MM, 0), W, locked=True)]
        self.assertEqual(g.normalize(dup).delete, [])

    def test_normalize_independent_of_input_order(self):
        segs = [seg(i, a, b) for i, a, b in FAULT] + polyline_segs(staircase((0, 0), 5), "q")
        segs += [seg("qx", (0, 0), (125_000, 0))]
        ref = g.normalize(segs)
        for k in range(5):
            shuffled = list(segs)
            random.Random(k).shuffle(shuffled)
            got = g.normalize(shuffled)
            self.assertEqual(
                (got.segments, got.delete, got.modify), (ref.segments, ref.delete, ref.modify)
            )

    def test_merge_direction_independent_of_segment_id_and_drawn_end(self):
        # N3's merged segment must be the same whichever original segment is kept as the
        # survivor (an accident of KiCad's per-process random track uuid, pnr.gloss.uid) and
        # whichever end that survivor happened to be drawn from. far is already sorted
        # (the one geometry fact), so the merge must always emit (far[0], far[1]).
        left, mid, right = (0, 0), (3 * MM, 0), (10 * MM, 0)

        def merge(id_left, id_right, flip_left, flip_right):
            a = seg(id_left, mid, left) if flip_left else seg(id_left, left, mid)
            b = seg(id_right, right, mid) if flip_right else seg(id_right, mid, right)
            return g.normalize([a, b])

        seen = set()
        for id_left, id_right in (("a", "z"), ("z", "a")):
            for flip_left in (False, True):
                for flip_right in (False, True):
                    norm = merge(id_left, id_right, flip_left, flip_right)
                    self.assertEqual(norm.counts["merged"], 1)
                    (survivor,) = norm.segments
                    seen.add((survivor.a, survivor.b))
        self.assertEqual(seen, {(left, right)})

    def test_staircase_normalize_drops_segments_not_copper(self):
        segs = []
        for k, (a, b) in enumerate(zip(STAIRS, STAIRS[1:])):
            mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            if g.direction(a, mid) == g.direction(mid, b) and g.direction(a, b) is not None:
                segs += [seg("L%03da" % k, a, mid), seg("L%03db" % k, mid, b)]
            else:
                segs.append(seg("L%03d" % k, a, b))
        norm = g.normalize(segs)
        self.assertGreater(len(segs), len(STAIRS) - 1)
        self.assertEqual(len(norm.segments), len(STAIRS) - 1)
        self.assertEqual(norm.counts["merged"], len(segs) - len(STAIRS) + 1)


# ======================================================================= dekink
class DekinkTest(unittest.TestCase):
    def test_staircase_route(self):
        pts = shift(STAIRS, -6 * MM, -4 * MM)
        self.assertEqual((len(pts) - 1, g.bends(pts)), (42, 41))
        new = g.dekink(pts, g.Context(W))
        self.assertIsNotNone(new)
        self.assertLessEqual(g.bends(new), 12)
        self.assertAlmostEqual(g.length(new), g.length(pts), delta=1)
        self.assertEqual((new[0], new[-1]), (pts[0], pts[-1]))
        self.assertLessEqual(g.sharp_turns(new), g.sharp_turns(pts))
        self.assertTrue(all(g.octilinear(a, b) for a, b in zip(new, new[1:])))
        # dekink alone stops at the cone changes; gloss (pull-tight in the 1 mm tube) finishes
        final = g.gloss(new, g.Context(W))
        self.assertLessEqual(g.bends(final), 5)
        self.assertLess(g.length(final), g.length(pts))

    def test_staircase_to_one_bend_equal_length(self):
        pts = [(-MM, 0)] + staircase((0, 0), 9) + [(staircase((0, 0), 9)[-1][0], -10 * MM)]
        new = g.dekink(pts, g.Context(W))
        self.assertAlmostEqual(g.length(new), g.length(pts), delta=1)
        self.assertEqual(g.bends(new), 2)
        self.assertLessEqual(g.sharp_turns(new), g.sharp_turns(pts))

    def jog(self):
        # E 1 mm, NE 1 mm, E 1 mm between anchors, and anchoring stubs
        return [(0, 0), (MM, 0), (2 * MM, -MM), (3 * MM, -MM)]

    def test_jog_blocked_corner_takes_alternative(self):
        pts = self.jog()
        free = g.dekink(pts, g.Context(W))
        self.assertEqual(g.bends(free), 1)
        self.assertEqual(
            free, [(0, 0), (2 * MM, 0), (3 * MM, -MM)]
        )  # least-swept tie: corner (2, 0)
        # a foreign via beside the corner (2, 0) blocks that alternative; the other corner remains
        w = World().via("x", (2_200_000, -100_000), 100_000)
        self.assertTrue(all(w.clear_for("n")(a, b) for a, b in zip(pts, pts[1:])))
        new = g.dekink(pts, w.ctx("n"))
        self.assertEqual(new, [(0, 0), (MM, -MM), (3 * MM, -MM)])
        # both corners blocked: no change
        w.via("x", (900_000, -600_000), 10_000)
        self.assertTrue(all(w.clear_for("n")(a, b) for a, b in zip(pts, pts[1:])))
        self.assertIsNone(g.dekink(pts, w.ctx("n")))

    def test_obstacle_inside_parallelogram_no_change(self):
        pts = self.jog()
        # test points (L3) on both sides of the jog, with a permissive clear(): only homotopy blocks
        ctx = g.Context(W, obstacles=[(1_600_000, -100_000), (1_400_000, -900_000)])
        self.assertIsNone(g.dekink(pts, ctx))
        ctx = g.Context(W, obstacles=[(1_600_000, -100_000)])
        new = g.dekink(pts, ctx)
        self.assertEqual(g.bends(new), 1)
        self.assertEqual(g.swept_points(pts, new, ctx.obstacles), [])

    def test_no_acute_and_anchors_fixed(self):
        # arrive heading south, then a {E, NE} staircase: starting the run NE would turn 135 deg
        pts = [(0, -3 * MM), (0, 0)]
        for k in range(8):
            d = (250_000, 0) if k % 2 == 0 else (250_000, -250_000)
            pts.append((pts[-1][0] + d[0], pts[-1][1] + d[1]))
        pts.append((pts[-1][0] + 2 * MM, pts[-1][1]))
        pts = g.simplify(pts)
        new = g.dekink(pts, g.Context(W))
        self.assertEqual(new, [(0, -3 * MM), (0, 0), (3 * MM, 0), (4 * MM, -MM)])
        self.assertEqual(g.turns(new), [90, 45])
        self.assertAlmostEqual(g.length(new), g.length(pts), delta=1)
        self.assertLessEqual(g.sharp_turns(new), g.sharp_turns(pts))
        self.assertEqual((new[0], new[-1]), (pts[0], pts[-1]))
        pts = self.jog()
        # acid trap at an anchor: another same-net leg leaves the start anchor towards north-east
        pts = self.jog()
        # a same-net leg leaving the end anchor southward: ending SW-bound would make a 45 deg trap
        legs = {pts[-1]: [(0, 1000)]}
        self.assertEqual(
            g.dekink(pts, g.Context(W, anchor_legs=legs)), [(0, 0), (MM, -MM), (3 * MM, -MM)]
        )
        # a leg leaving the start anchor northward: starting NE would make a 45 deg trap
        legs = {pts[0]: [(0, -1000)]}
        self.assertEqual(
            g.dekink(pts, g.Context(W, anchor_legs=legs)), [(0, 0), (2 * MM, 0), (3 * MM, -MM)]
        )
        legs = {pts[0]: [(0, -1000)], pts[-1]: [(0, 1000)]}  # both corners refused
        self.assertIsNone(g.dekink(pts, g.Context(W, anchor_legs=legs)))

    def test_turns_ok_rejects_new_acid_trap(self):
        old = [(0, 0), (MM, 0), (2 * MM, -MM)]
        new = [(0, 0), (MM, -MM), (2 * MM, -MM)]
        self.assertTrue(g.turns_ok(old, new, {(0, 0): [(-1, 0)]}))  # 135 deg to a leg going west
        self.assertFalse(
            g.turns_ok(old, new, {(0, 0): [(0, -1)]})
        )  # 45 deg to a leg going north (old 90)
        self.assertTrue(g.turns_ok(old, new, {(0, 0): [(1, -2)]}) is False)
        # a new interior turn sharper than 90 deg is refused; an existing one may stay
        self.assertFalse(
            g.turns_ok([(0, 0), (MM, 0), (MM, MM)], [(0, 0), (MM, 0), (0, MM // 2), (MM, MM)])
        )
        sharp = [(0, 0), (2 * MM, 0), (MM, MM), (MM, 2 * MM)]
        self.assertTrue(g.turns_ok(sharp, sharp))

    def test_dekink_deterministic_and_shuffle_free(self):
        pts = shift(STAIRS, -6 * MM, -4 * MM)
        a = g.dekink(pts, g.Context(W))
        b = g.dekink(list(pts), g.Context(W))
        self.assertEqual(a, b)


# ======================================================================= gloss
class GlossTest(unittest.TestCase):
    def test_u_turn_removed(self):
        pts = shift(UTURN, -4 * MM, -3 * MM)
        new = g.gloss(pts, g.Context(W))
        self.assertIsNotNone(new)
        self.assertLess(g.length(new), g.length(pts) - MM)
        self.assertNotIn(135, [g.turn_class(t) for t in g.turns(new)])
        # no heading reversal (U) left: total absolute turning stays below 180
        self.assertLess(sum(g.turns(new)), 180)
        tube = g._Tube(g.simplify(pts), g.TUBE)
        self.assertTrue(all(tube.covers(a, b) for a, b in zip(new, new[1:])))

    def test_homotopy_keeps_side_of_foreign_via(self):
        # detour above a via; the straight shortcut would put the via on the other side
        pts = [(0, 0), (0, -MM), (2 * MM, -MM), (2 * MM, 0)]
        self.assertEqual(g.gloss(pts, g.Context(W)), [(0, 0), (2 * MM, 0)])
        w = World().via("x", (MM, -300_000), 20_000)
        new = g.gloss(pts, w.ctx("n"))
        self.assertIsNotNone(new)
        self.assertLess(g.length(new), g.length(pts))
        self.assertEqual(g.swept_points(pts, new, w.points("n")), [])
        self.assertEqual(winding_side(new, (MM, -300_000)), -1)  # the via is still under the path
        # a bare L3 test point (no clearance) is enough to keep the side
        new = g.gloss(pts, g.Context(W, obstacles=[(MM, -300_000)]))
        self.assertEqual(new, [(0, 0), (MM, -MM), (2 * MM, 0)])
        self.assertEqual(winding_side(new, (MM, -300_000)), -1)

    def test_tube_limit(self):
        # a wide U: the chord is > 2T away from the bottom, so the shortcut must stay near the arms
        pts = [(0, 0), (0, 6 * MM), (5 * MM, 6 * MM), (5 * MM, 0)]
        new = g.gloss(pts, g.Context(W))
        tube = g._Tube(pts, g.TUBE)
        self.assertIsNotNone(new)
        self.assertTrue(all(tube.covers(a, b) for a, b in zip(new, new[1:])))
        self.assertNotEqual(new, [(0, 0), (5 * MM, 0)])

    def test_equal_cost_no_change(self):
        self.assertIsNone(g.gloss([(0, 0), (MM, 0), (2 * MM, MM)], g.Context(W)))
        self.assertIsNone(g.gloss([(0, 0), (3 * MM, 0)], g.Context(W)))
        self.assertIsNone(g.gloss([(0, 0), (MM, MM), (2 * MM, MM)], g.Context(W)))
        self.assertIsNone(g.dekink([(0, 0), (MM, 0), (2 * MM, MM)], g.Context(W)))

    def test_cost_never_rises_keyhole_measure(self):
        rng = random.Random(11)
        changed = 0
        for _ in range(40):
            pts = [(0, 0)]
            for _ in range(rng.randint(2, 9)):
                d = g.DIRS[rng.choice((0, 1, 2, 7, 6))]
                n = rng.randint(1, 6) * 250_000
                pts.append((pts[-1][0] + d[0] * n, pts[-1][1] + d[1] * n))
            pts = g.simplify(pts)
            w = World()
            for _ in range(rng.randint(0, 4)):
                c = (rng.randint(-MM, 6 * MM), rng.randint(-6 * MM, 6 * MM))
                if all(g.point_segment_distance(c, a, b) > 600_000 for a, b in zip(pts, pts[1:])):
                    w.via("x", c, 100_000)
            new = g.gloss(pts, w.ctx("n", bounds=(-20 * MM, -20 * MM, 20 * MM, 20 * MM)))
            if new is None:
                continue
            changed += 1

            def mmp(q):
                return [(p[0] / MM, p[1] / MM) for p in q]

            c_old = keyhole.length(mmp(pts)) + 0.15 * g.bends(pts)
            c_new = keyhole.length(mmp(new)) + 0.15 * g.bends(new)
            self.assertLessEqual(c_new, c_old + 1e-9)
            self.assertLessEqual(g.length(new), g.length(pts) + g.LENGTH_TOL)
            self.assertEqual(g.swept_points(pts, new, w.points("n")), [])
            clear = w.clear_for("n")
            self.assertTrue(
                all(clear(a, b) for a, b in zip(new, new[1:]) if (a, b) not in zip(pts, pts[1:]))
            )
        self.assertGreater(changed, 20)

    def test_string_pull_only_matches_relax_in_free_space(self):
        pts = [(0, 0), (MM, 0), (MM, MM), (2 * MM, MM), (2 * MM, 2 * MM)]
        ours = g.gloss(pts, g.Context(W), use_tube=False)
        theirs = keyhole.relax([(p[0] / MM, p[1] / MM) for p in pts], lambda a, b: True)
        self.assertAlmostEqual(g.length(ours) / MM, keyhole.length(theirs), places=6)

    def test_window_mode_for_long_chains(self):
        pts = [(0, 0)] + staircase((0, 0), 140) + [(staircase((0, 0), 140)[-1][0] + MM, 0)]
        pts = g.simplify(pts)
        self.assertGreater(len(pts) - 1, g.WINDOW_MAX_LEGS)
        new = g.gloss(pts, g.Context(W))
        self.assertIsNotNone(new)
        self.assertLess(g.bends(new), g.bends(pts))
        self.assertEqual((new[0], new[-1]), (pts[0], pts[-1]))

    def test_l_corner_chamfered_in_tube(self):
        pts = [(0, 0), (0, -3 * MM), (3 * MM, -3 * MM)]
        new = g.gloss(pts, g.Context(W))
        self.assertEqual(new, [(0, 0), (0, -MM), (2 * MM, -3 * MM), (3 * MM, -3 * MM)])
        self.assertEqual(g.bend_histogram(new)[45], 2)
        self.assertLess(g.cost(new), g.cost(pts))

    def test_guards_g1_g2(self):
        # a pair just inside the corner: the free chamfer would come within 0.012 mm of it
        pts = [(0, 0), (0, -3 * MM), (3 * MM, -3 * MM)]
        pair = g.Guard(
            "pair", (((900_000, -1_600_000), (1_300_000, -1_200_000), 100_000),), 450_000
        )

        def segs(p):
            return list(zip(p, p[1:]))

        free = g.gloss(pts, g.Context(W))
        self.assertLess(g.guard_distance(segs(free), W, pair), 450_000)
        guarded = g.gloss(pts, g.Context(W, guards=[pair]))
        self.assertIsNotNone(guarded)
        self.assertLess(g.cost(guarded), g.cost(pts))
        self.assertGreater(g.cost(guarded), g.cost(free))
        self.assertGreaterEqual(
            min(g.guard_distance(segs(guarded), W, pair), 450_000),
            min(g.guard_distance(segs(pts), W, pair), 450_000) - 1,
        )
        # G2 (open terminal pad edges, cap 1 mm) uses the same rule
        pad = ((1_100_000, -1_900_000), (1_100_000, -1_900_000), 150_000)
        g2 = g.Guard("open_terminal", (pad,), 1_000_000)
        guarded = g.gloss(pts, g.Context(W, guards=[g2]))
        if guarded is not None:
            self.assertGreaterEqual(
                min(g.guard_distance(segs(guarded), W, g2), 1_000_000),
                min(g.guard_distance(segs(pts), W, g2), 1_000_000) - 1,
            )
        self.assertNotEqual(guarded, free)


def winding_side(path, p):
    """Sign of the side of p relative to a path that runs from (0,0) to (2mm,0) through y<0."""
    loop = path + [(path[-1][0], 10 * MM), (path[0][0], 10 * MM)]
    return -1 if g.winding_number(loop, p) else 1


# ======================================================================= corridor
def pi_chain(cid, net, y, xs, xe, top=0, kind="v", w=W, c=C, key="K", movable=True, si=False):
    if kind == "v":
        pts = [(xs, top), (xs, y), (xe, y), (xe, top)]
    else:  # 45-degree neighbours, up-left and up-right
        t = y - top
        pts = [(xs - t, top), (xs, y), (xe, y), (xe + t, top)]
    k = None if key is None else g.class_key("In2.Cu", key, w, c)
    return g.CorridorChain(cid, net, w, c, pts, k, movable, si)


def corridor(chains, world=None, **kw):
    world = world or World()

    def clear(net, width, a, b):
        return world.clear_for(net, width)(a, b)

    return g.Corridor(chains, layer="In2.Cu", clear=clear, obstacles=world.points(), **kw)


def horizontal(stacks):
    return [st for st in stacks if st[0].d4 == 0]


def apply(chains, edit):
    state = g.apply_corridor(chains, edit)
    return [
        g.CorridorChain(c.id, c.net, c.width, c.clearance, state[c.id], c.key, c.movable, c.si)
        for c in chains
    ]


class CorridorTest(unittest.TestCase):
    def test_two_legs_pack_to_min_pitch(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)  # centre 1.0 mm, edge gap 0.8 mm
        cor = corridor([a, b])
        self.assertEqual(len(horizontal(cor.stacks())), 1)
        self.assertEqual(len(cor.stacks()), 3)  # the arms stack too, but are pinned
        edits = cor.plan()
        self.assertEqual(len(edits), 1)
        e = edits[0]
        self.assertEqual(e.extra["anchor"]["net"], "A")
        (m,) = e.members
        self.assertEqual(m["net"], "B")
        self.assertEqual(m["new"][1][1] - 5 * MM, 328_000)
        self.assertAlmostEqual(m["dL"], -2 * (MM - 328_000))
        self.assertAlmostEqual(e.extra["dE_mm2"], -8.0 * 0.672, places=6)
        # the pinned arms (two 5 mm pairs at 0.8 mm) keep their excess; only the member pair is packed
        self.assertAlmostEqual(e.extra["E_new_mm2"], 2 * 5 * 0.672, places=6)
        self.assertAlmostEqual(e.extra["E_mm2"] - e.extra["E_new_mm2"], 8 * 0.672, places=6)
        spec = e.to_spec()
        self.assertEqual(spec["members"][0]["shift_mm"], -0.672)

    def test_stacks_keep_order_3_to_5(self):
        for m in (3, 4, 5):
            chains = [
                pi_chain("c%d" % i, "N%d" % i, (5 + 0.9 * i) * MM, (2 - i) * MM, (12 + i) * MM)
                for i in range(m)
            ]
            edits = corridor(chains).plan()
            self.assertEqual(len(edits), 1, m)
            after = apply(chains, edits[0])
            ys = [c.points[1][1] for c in after]
            self.assertEqual(ys, sorted(ys))
            for y0, y1 in zip(ys, ys[1:]):
                self.assertGreaterEqual(y1 - y0, 328_000)
            self.assertEqual(ys[1] - ys[0], 328_000)

    def test_anchor_choice_deterministic(self):
        chains = [
            pi_chain(
                "c%d" % i, "N%d" % i, (5 + 0.8 * i) * MM, (2 - i) * MM, (12 + i) * MM, kind="d"
            )
            for i in range(4)
        ]
        first = corridor(chains).plan()[0].to_spec()
        for seed in range(5):
            shuffled = list(chains)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(corridor(shuffled).plan()[0].to_spec(), first)

    def test_anchored_member_immovable(self):
        k = g.class_key("In2.Cu", "K", W, C)
        # A: arms go down outside B; B's horizontal is its first leg (its start is the chain anchor)
        a = g.CorridorChain(
            "a", "A", W, C, [(0, 12 * MM), (0, 5 * MM), (12 * MM, 5 * MM), (12 * MM, 12 * MM)], k
        )
        b = g.CorridorChain(
            "b", "B", W, C, [(MM, 6 * MM), (11 * MM, 6 * MM), (11 * MM, 12 * MM)], k
        )
        cor = corridor([a, b])
        (stack,) = horizontal(cor.stacks())
        base = {c.id: c.points for c in (a, b)}
        _, moves, reasons = cor._pack(stack, 0, base)  # anchor A: B would have to move
        self.assertEqual((moves, {m.net: r for m, r in reasons.items()}), ({}, {"B": "anchored"}))
        edits = cor.plan()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].extra["anchor"]["net"], "B")
        (m,) = edits[0].members
        self.assertEqual((m["net"], m["new"][1][1]), ("A", 6 * MM - 328_000))
        self.assertEqual(m["new"][0], (0, 12 * MM))
        # both pinned: nothing to do
        a2 = g.CorridorChain("a", "A", W, C, [(2 * MM, 5 * MM), (10 * MM, 5 * MM), (10 * MM, 0)], k)
        self.assertEqual(corridor([a2, b]).plan(), [])

    def test_drag_lengths_exact(self):
        for kind, per_end in (("v", lambda d: -d), ("d", lambda d: -(math.sqrt(2) - 1) * d)):
            a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM, kind=kind)
            b = pi_chain("b", "B", 6 * MM, MM, 11 * MM, kind=kind)
            (m,) = corridor([a, b]).plan()[0].members
            d = 1 * MM - 328_000
            self.assertAlmostEqual(m["dL"], 2 * per_end(d), delta=1e-6)
            self.assertAlmostEqual(g.length(m["new"]) - g.length(m["old"]), m["dL"], delta=1e-6)
            self.assertEqual(m["dB"], 0)

    def test_diagonal_stack_pitch(self):
        # nested Pi shapes in the frame u = (1, 1), v = (-1, 1): members run NE, arms are perpendicular
        unit = 500_000
        k = g.class_key("In2.Cu", "K", W, C)

        def rot(u, v):
            return (round((u - v) * unit), round((u + v) * unit))

        def chain(cid, net, xs, xe, y):
            return g.CorridorChain(
                cid, net, W, C, [rot(xs, 0), rot(xs, y), rot(xe, y), rot(xe, 0)], k
            )

        a, b = chain("a", "A", 1, 5, 2), chain("b", "B", 0, 6, 3)
        edits = corridor([a, b]).plan()
        self.assertEqual(len(edits), 1)
        after = apply([a, b], edits[0])
        legs = [c.points[1:3] for c in after]
        d = g.segment_distance(legs[0][0], legs[0][1], legs[1][0], legs[1][1])
        self.assertGreaterEqual(d, 328_000 - 1e-6)
        self.assertLess(d, 329_000)
        for c in after:
            self.assertTrue(all(g.octilinear(p, q) for p, q in zip(c.points, c.points[1:])))
            self.assertEqual(c.points[0], [x for x in (a, b) if x.id == c.id][0].points[0])
        (m,) = edits[0].members
        self.assertEqual(m["net"], "B")
        self.assertLess(m["dL"], 0)

    def test_pad_in_strip_blocks(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)
        w = World().via("P", (6 * MM, 5 * MM + 500_000), 150_000)
        cor = corridor(
            [a, b],
            w,
            strip_blocked=lambda quad, nets: any(
                g.point_in_polygon(c, quad)
                or min(g.segment_distance(c, c, quad[i], quad[(i + 1) % 4]) for i in range(4)) < r
                for n, c, r in w.discs
                if n not in nets
            ),
        )
        self.assertEqual(horizontal(cor.stacks()), [])
        self.assertEqual(cor.plan(), [])

    def test_track_in_strip_blocks(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)
        cor = corridor(
            [a, b], tracks=[("P", (5 * MM, 5 * MM + 500_000), (7 * MM, 5 * MM + 500_000), W)]
        )
        self.assertEqual(horizontal(cor.stacks()), [])
        self.assertEqual(len(horizontal(corridor([a, b]).stacks())), 1)

    def test_class_mismatch_never_grouped(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        logic = pi_chain("b", "B", 6 * MM, MM, 11 * MM, w=500_000, key="logic")
        self.assertEqual(corridor([a, logic]).stacks(), [])
        self.assertEqual(corridor([a, logic]).plan(), [])
        pair = pi_chain("b", "Dp", 6 * MM, MM, 11 * MM, key=None, movable=False)
        self.assertEqual(corridor([a, pair]).stacks(), [])
        wider_c = pi_chain("b", "B", 6 * MM, MM, 11 * MM, c=150_000)
        self.assertEqual(corridor([a, wider_c]).stacks(), [])

    def test_g1_refuses_move_toward_pair(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)
        # pair legs between A's and B's right arms, ending 0.4 mm above B, outside the A-B strip
        pair = g.Guard(
            "pair",
            (
                ((10_400_000, 0), (10_400_000, 5_600_000), 100_000),
                ((10_750_000, 0), (10_750_000, 5_600_000), 100_000),
            ),
            450_000,
        )
        self.assertEqual(len(horizontal(corridor([a, b]).stacks())), 1)
        self.assertEqual(len(corridor([a, b]).plan()), 1)  # B would move up 0.672 mm
        cor = corridor([a, b], guards=lambda cid: [pair])
        # ...toward the pair: refused. Only A may pack (partially: its 0.672 mm drag costs
        # 1.344 mm of length; the largest shift within the 0.2 mm slack is 0.1 mm).
        plans = cor.plan()
        self.assertEqual([[m["net"] for m in e.members] for e in plans], [["A"]])
        (m,) = plans[0].members
        self.assertTrue(m["partial"])
        self.assertEqual((m["shift"], m["dL"]), (100_000, 200_000))
        _, moves, reasons = cor._pack(
            horizontal(cor.stacks())[0], 0, {c.id: c.points for c in (a, b)}
        )
        self.assertEqual({m.net: r for m, r in reasons.items()}, {"B": "L7:pair"})
        # the same pair below B: B moving up goes away from it and is allowed
        below = g.Guard("pair", (((0, 6_700_000), (12 * MM, 6_700_000), 100_000),), 450_000)
        self.assertEqual(len(corridor([a, b], guards=lambda cid: [below]).plan()), 1)

    def test_si_leg_is_boundary(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        si = pi_chain("s", "S", 6 * MM, MM, 11 * MM, movable=False, si=True)
        c = pi_chain("c", "C", 7 * MM, 0, 12 * MM)
        cor = corridor([a, si, c])
        self.assertEqual(horizontal(cor.stacks()), [])  # S sits between A and C and is not a member
        # SI to the side: A may pack to B but never toward S
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)
        si2 = pi_chain("s", "S", 4 * MM, 3 * MM, 9 * MM, movable=False, si=True)
        edits = corridor([si2, a, b]).plan()
        self.assertEqual(len(edits), 1)
        self.assertEqual([m["net"] for m in edits[0].members], ["B"])

    def test_random_property_no_crossing_and_gain(self):
        produced = 0
        for seed in range(200):
            rng = random.Random(seed)
            m = rng.randint(2, 5)
            kind = rng.choice("vd")
            y = 5 * MM
            chains = []
            xs, xe = 3 * MM, 12 * MM
            for i in range(m):
                chains.append(pi_chain("c%d" % i, "N%d" % i, y, xs, xe, kind=kind))
                y += W + rng.randint(150_000, 1_900_000)
                step = rng.randint(400_000, 1_200_000)
                xs -= step
                xe += step
            world = World()
            for _ in range(rng.randint(0, 3)):
                c = (rng.randint(-5 * MM, 20 * MM), rng.randint(0, y))
                if all(
                    g.point_segment_distance(c, p, q) > W / 2 + C + g.MARGIN + 200_000
                    for ch in chains
                    for p, q in zip(ch.points, ch.points[1:])
                ):
                    world.via("V", c, 150_000)
            for _round in range(2):
                cor = corridor(chains, world)
                edits = cor.plan()
                if not edits:
                    break
                e = edits[0]
                self.assertLessEqual(e.extra["dE_mm2"], -0.05)
                for mem in e.members:
                    self.assertLessEqual(mem["dB"], 0)
                    self.assertLessEqual(mem["dL"], g.CORRIDOR_SLACK + 1e-6)
                    self.assertEqual(
                        (mem["new"][0], mem["new"][-1]), (mem["old"][0], mem["old"][-1])
                    )
                before = sorted(chains, key=lambda c: c.points[1][1])
                chains = apply(chains, e)
                produced += 1
                order = sorted(chains, key=lambda c: c.points[1][1])
                self.assertEqual([c.id for c in before], [c.id for c in order], seed)
                for i, c1 in enumerate(chains):
                    for c2 in chains[i + 1 :]:
                        for p, q in zip(c1.points, c1.points[1:]):
                            for r, s in zip(c2.points, c2.points[1:]):
                                self.assertFalse(g.segments_intersect(p, q, r, s), seed)
                                self.assertGreaterEqual(
                                    g.segment_distance(p, q, r, s), 328_000 - 1e-6, seed
                                )
                    clear = world.clear_for(c1.net)
                    self.assertTrue(
                        all(clear(p, q) for p, q in zip(c1.points, c1.points[1:])), seed
                    )
        self.assertGreater(produced, 150)


# ======================================================================= adjacency, anchor turns, budgets
K_DEF = g.class_key("In2.Cu", "Default", W, C)


def ray_world(items):
    """RayIndex of same-class neighbour tracks [(net, a, b)] plus hug/snap callables for net 'n'."""
    rays = [g.RayItem(a, b, W / 2, net, K_DEF, C, "w%d" % i) for i, (net, a, b) in enumerate(items)]
    index = g.RayIndex(rays)

    def hug(a, b):
        return g.hug_segment(a, b, W / 2, "n", K_DEF, C, index)

    def snap(box):
        return [r for r in index.near(box) if r.net != "n" and r.key == K_DEF]

    return index, hug, snap


class RefinementTest(unittest.TestCase):
    def test_rule_r_charges_turns_at_end_anchors(self):
        old = [(0, 0), (MM, -MM), (3 * MM, -MM)]
        new = [(0, 0), (2 * MM, 0), (3 * MM, -MM)]
        legs = {(0, 0): [(-MM, 0)]}  # the outside leg arrives heading east
        self.assertEqual(g.end_turns(old, legs), [45])
        self.assertEqual(g.end_turns(new, legs), [0])
        self.assertEqual((g.total_bends(old, legs), g.total_bends(new, legs)), (2, 1))
        self.assertFalse(g.rule_r(old, new))  # interior bends alone: 1 -> 1
        self.assertTrue(g.rule_r(old, new, legs))
        self.assertFalse(g.rule_r(new, old, legs))
        # a junction (two outside legs) or a pad end (none) has no turn to charge
        self.assertEqual(g.end_turns(old, {(0, 0): [(-MM, 0), (0, MM)]}), [])
        self.assertEqual(g.end_turns(old, {}), [])

    def test_dekink_counts_anchor_turns(self):
        # the staircase leaves an anchor whose outside leg arrives heading north-east: ending the
        # chain's first leg north-east costs no bend there, starting east would
        pts = [(0, 0), (MM, 0), (2 * MM, -MM), (3 * MM, -MM)]
        legs = {(0, 0): [(-MM, MM)]}
        new = g.dekink(pts, g.Context(W, anchor_legs=legs))
        self.assertEqual(new, [(0, 0), (MM, -MM), (3 * MM, -MM)])
        self.assertLess(g.total_bends(new, legs), g.total_bends(pts, legs))

    def test_adjacency_metric_parallel_tight_and_blocked(self):
        a = g.RayItem((0, 0), (10 * MM, 0), W / 2, "A", K_DEF, C, "a")
        b = g.RayItem((0, MM), (10 * MM, MM), W / 2, "B", K_DEF, C, "b")  # edge gap 0.8 mm
        index = g.RayIndex([a, b])
        x = g.adjacency([a, b], index)
        self.assertAlmostEqual(x["X"] / MM / MM, 2 * 10 * (0.8 - 0.128), places=3)
        self.assertEqual(x["T"], 0)
        tight = g.RayItem((0, 328_000), (10 * MM, 328_000), W / 2, "B", K_DEF, C, "b")
        x = g.adjacency([a, tight], g.RayIndex([a, tight]))
        self.assertAlmostEqual(x["T"] / MM, 20, places=3)
        self.assertEqual(x["X"], 0)
        self.assertAlmostEqual(x["pairs"][("A", "B")] / MM, 20, places=3)
        # a via between them stops the rays: no same-class adjacency counted over it
        via = g.RayItem((5 * MM, 500_000), (5 * MM, 500_000), 300_000, "V", None, C, "v")
        x = g.adjacency([a, b], g.RayIndex([a, b, via]))
        self.assertLess(x["X"] / MM / MM, 2 * 10 * (0.8 - 0.128) - 0.5)
        # other class / other angle: not adjacency
        other = g.RayItem(
            (0, MM), (10 * MM, MM), W / 2, "B", ("In2.Cu", "logic", 500_000, C), C, "b"
        )
        self.assertEqual(g.adjacency([a], g.RayIndex([a, other]))["X"], 0)
        diag = g.RayItem((0, MM), (5 * MM, 6 * MM), W / 2, "B", K_DEF, C, "b")
        self.assertEqual(g.adjacency([a], g.RayIndex([a, diag]))["X"], 0)
        # window: only samples inside count
        x = g.adjacency([a, b], index, window=(0, -MM, 5 * MM, 2 * MM))
        self.assertAlmostEqual(x["X"] / MM / MM, 10 * (0.8 - 0.128), places=3)

    def test_adjacency_does_not_depend_on_drawing_direction(self):
        # The neighbour turns away right above the samples at 5.65 and 5.75 mm: their rays meet
        # the common end cap of its parallel leg and of its perpendicular leg at the same
        # distance. Whichever way the subject and the neighbour's legs are drawn, X, T and the
        # hug score are the same, and those samples count the neighbour: X equals that of the
        # parallel leg alone.
        subject = ((0, 0), (7_500_000, 0))
        bend = (5_750_000, 1_750_000)
        legs = [(bend, (7_500_000, 1_750_000)), (bend, (5_750_000, 4_000_000))]

        def measure(flip, flips, legs=legs):
            a, b = subject[::-1] if flip else subject
            items = [g.RayItem(a, b, W / 2, "A", K_DEF, C, "a")]
            for k, ((p, q), f) in enumerate(zip(legs, flips)):
                p, q = (q, p) if f else (p, q)
                items.append(g.RayItem(p, q, W / 2, "B", K_DEF, C, "b%d" % k))
            index = g.RayIndex(items)
            x = g.adjacency(items[:1], index)
            hug = g.hug_segment(a, b, W / 2, "A", K_DEF, C, index)
            return round(x["X"]), round(x["T"]), round(hug)

        seen = {
            measure(flip, (f1, f2))
            for flip in (False, True)
            for f1 in (False, True)
            for f2 in (False, True)
        }
        self.assertEqual(len(seen), 1, seen)
        X, T, _ = seen.pop()
        self.assertEqual(T, 0)
        self.assertGreater(X, 0)
        self.assertEqual(X, measure(False, (False,), legs[:1])[0])

    def test_dekink_snaps_to_same_class_neighbour(self):
        # staircase in the {E, NE} cone; the outside legs at both anchors run north-east, so every
        # fewest-bend path is NE - E - NE (2 bends) and only the E leg's height is free
        old = [
            (0, 0),
            (2 * MM, 0),
            (3 * MM, -MM),
            (6_750_000, -MM),
            (7_750_000, -2 * MM),
            (9 * MM, -2 * MM),
        ]
        legs = {(0, 0): [(-MM, MM)], (9 * MM, -2 * MM): [(10 * MM, -3 * MM)]}
        legs = {(0, 0): [(-MM, MM)], (9 * MM, -2 * MM): [(MM, -MM)]}
        neighbour = ("X", (3_500_000, -1_600_000), (6_500_000, -1_600_000))
        w = World().track("X", neighbour[1], neighbour[2])
        index, hug, snap = ray_world([neighbour])
        plain = g.dekink(old, w.ctx("n", anchor_legs=legs))
        self.assertEqual(g.total_bends(plain, legs), 2)
        ctx = w.ctx("n", anchor_legs=legs, hug=hug, snap=snap, clearance=C)
        new = g.dekink(old, ctx)
        self.assertEqual(g.total_bends(new, legs), 2)
        self.assertAlmostEqual(g.length(new), g.length(old), delta=1)
        # the E leg sits at minimum pitch (0.328 mm) below the neighbour: y = -1.6 + 0.328
        flats = [(p, q) for p, q in zip(new, new[1:]) if p[1] == q[1]]
        self.assertEqual([p[1] for p, _ in flats], [-1_272_000])
        self.assertLess(ctx.hug(new), ctx.hug(plain) - g.ADJ_MIN_GAIN)
        self.assertTrue(all(w.clear_for("n")(p, q) for p, q in zip(new, new[1:])))
        self.assertEqual(g.swept_points(old, new, w.points("n")), [])

    def test_small_gain_that_loosens_adjacency_is_declined(self):
        pts = [(0, 0), (MM, 0), (2 * MM, -MM), (3 * MM, -MM)]
        # the corner (2, 0) is blocked, so the only fewest-bend path climbs at once, away from a
        # same-class neighbour running 0.2 mm (edge) below the first leg
        w = World().via("x", (2_200_000, -100_000), 100_000)
        neighbour = ("X", (0, 400_000), (3 * MM, 400_000))
        w.track(*neighbour)
        index, hug, snap = ray_world([neighbour])
        self.assertEqual(g.dekink(pts, w.ctx("n")), [(0, 0), (MM, -MM), (3 * MM, -MM)])
        ctx = w.ctx("n", hug=hug, snap=snap, clearance=C)
        self.assertIsNone(g.dekink(pts, ctx))  # saves 0.15 mm (< 0.2 slack), hugs less
        self.assertEqual(ctx.why, "hug:declined")

    def test_corridor_partial_shift_and_next_member_packs_against_it(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 10 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 11 * MM)
        c = pi_chain("c", "C", 7 * MM, 0, 12 * MM)
        w = World().via("V", (10_600_000, 5_550_000), 100_000)  # beside the A-B strip, above B
        cor = corridor([a, b, c], w)
        (stack,) = [st for st in horizontal(cor.stacks()) if len(st) == 3]
        base = {x.id: g.simplify(x.points) for x in (a, b, c)}
        state, moves, reasons = cor._pack(stack, 0, base)
        got = {m.net: v for m, v in moves.items()}
        self.assertTrue(got["B"]["partial"])
        self.assertEqual(got["B"]["new"][1][1], 5_878_000)  # stops at pitch from the via
        self.assertFalse(got["C"]["partial"])
        self.assertEqual(got["C"]["new"][1][1] - got["B"]["new"][1][1], 328_000)
        self.assertTrue(
            all(
                w.clear_for(x.net)(p, q)
                for x in (b, c)
                for p, q in zip(state[x.id], state[x.id][1:])
            )
        )

    def test_search_budget_is_deterministic(self):
        pts = [(0, 0), (0, -3 * MM), (3 * MM, -3 * MM)]
        runs = []
        for _ in range(2):
            ctx = g.Context(W, search_budget=5)
            out = g.tube_search(pts, ctx)
            runs.append((out, dict(ctx.calls)))
            self.assertLessEqual(ctx.calls["search"], 5)
            self.assertGreater(ctx.calls["search_refused"], 0)
            self.assertEqual(ctx.why, "search_budget")
        self.assertEqual(runs[0], runs[1])


# ======================================================================= metrics
class MetricTest(unittest.TestCase):
    def test_excess_matches_survey_definition(self):
        k = g.class_key("In2.Cu", "Default", W, C)

        def leg(cid, net, a, b, key=k):
            return g.Leg(
                cid,
                net,
                key,
                W,
                C,
                0,
                a,
                b,
                g.direction(a, b) % 4 if g.direction(a, b) is not None else None,
                False,
                True,
            )

        s = leg("s", "S", (0, 0), (5 * MM, 0))
        t = leg("t", "T", (MM, MM), (6 * MM, MM))  # overlap 4 mm, edge gap 0.8 mm
        u = leg("u", "U", (0, 3 * MM), (5 * MM, 3 * MM + 800_000))  # ~9 deg: survey only
        tracks = [(x.net, x.a, x.b, W) for x in (s, t, u)]
        self.assertAlmostEqual(
            g.corridor_excess([s, t], tracks, mode="bbox") / MM / MM, 4 * (0.8 - 0.127)
        )
        self.assertAlmostEqual(g.corridor_excess([s, t], tracks) / MM / MM, 4 * (0.8 - 0.128))
        pairs_bbox = g.corridor_pairs([s, t, u], tracks, mode="bbox")
        pairs = g.corridor_pairs([s, t, u], tracks)
        self.assertEqual(len(pairs), 1)
        self.assertGreater(len(pairs_bbox), 1)
        # survey transcription on the same three legs
        self.assertAlmostEqual(
            sum(p["excess"] for p in pairs_bbox), survey_excess([s, t, u]), delta=1
        )

        # a via in the strip blocks the pair in both modes
        def blocked(quad, nets):
            return g.point_in_polygon((3 * MM, 500_000), quad)

        self.assertEqual(g.corridor_excess([s, t], tracks, blocked), 0)
        self.assertEqual(g.corridor_excess([s, t], tracks, blocked, mode="bbox"), 0)
        # already tight (gap <= c + 0.1) and too far (> 2 mm) do not count
        near = leg("n", "N", (0, 400_000), (5 * MM, 400_000))
        far = leg("f", "F", (0, -2_300_000), (5 * MM, -2_300_000))
        self.assertEqual(g.corridor_excess([s, near, far], []), 0)
        # windowed E counts a pair when either leg is in the window
        win = (5 * MM + 100_000, 900_000, 6 * MM, 1_100_000)  # touches t only
        self.assertAlmostEqual(
            g.corridor_excess([s, t], tracks, window=win) / MM / MM, 4 * (0.8 - 0.128)
        )
        self.assertEqual(g.corridor_excess([s, t], tracks, window=(20 * MM, 0, 21 * MM, MM)), 0)

    def test_measure(self):
        stairs = staircase((0, 0), 6)
        groups = {
            ("a", "In2.Cu"): polyline_segs(stairs, "a"),
            ("b", "In2.Cu"): polyline_segs([(0, MM), (5 * MM, MM)], "b"),
        }
        k = g.class_key("In2.Cu", "Default", W, C)

        def info(net, layer, width):
            return dict(key=k, si=False, eligible=True, clearance=C)

        m = g.measure(groups, info=info)
        (row,) = m.values()
        self.assertEqual(row["segments"], 7)
        self.assertEqual(row["bends"][45], 5)
        self.assertAlmostEqual(row["length_mm"], (g.length(stairs) + 5 * MM) / MM)
        self.assertGreaterEqual(row["excess_mm2"], 0)


def survey_excess(legs):
    """Independent transcription of the design survey's corridor pair loop (bounding boxes)."""
    total = 0.0
    for i, s in enumerate(legs):
        for t in legs[i + 1 :]:
            L = math.dist(s.a, s.b)
            hs = math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])) % 180
            ht = math.degrees(math.atan2(t.b[1] - t.a[1], t.b[0] - t.a[0])) % 180
            if min(abs(hs - ht), 180 - abs(hs - ht)) > 15:
                continue
            ux, uy = (s.b[0] - s.a[0]) / L, (s.b[1] - s.a[1]) / L
            pr = sorted(((q[0] - s.a[0]) * ux + (q[1] - s.a[1]) * uy) for q in (t.a, t.b))
            lo, hi = max(0, pr[0]), min(L, pr[1])
            if hi - lo <= 2 * MM:
                continue
            sa = (s.a[0] + ux * lo, s.a[1] + uy * lo)
            sb = (s.a[0] + ux * hi, s.a[1] + uy * hi)
            tv = (t.b[0] - t.a[0], t.b[1] - t.a[1])
            tdot = tv[0] * ux + tv[1] * uy

            def tat(u):
                f = ((u - ((t.a[0] - s.a[0]) * ux + (t.a[1] - s.a[1]) * uy)) / tdot) if tdot else 0
                return (t.a[0] + tv[0] * f, t.a[1] + tv[1] * f)

            ta, tb = tat(lo), tat(hi)
            edge = g.segment_distance(sa, sb, ta, tb) - (W + W) / 2
            if edge <= C + 100_000 or edge > 2 * MM:
                continue
            total += (hi - lo) * (edge - C)
    return total


# ======================================================================= edits and batching
class EditTest(unittest.TestCase):
    def test_spec_and_batches(self):
        segs = polyline_segs(shift(UTURN, -4 * MM, -3 * MM), "n")
        cs = g.build_chains(segs, net="n1", layer="F.Cu")
        (ch,) = cs.chains
        new = g.gloss(ch.points, g.Context(W))
        e = g.make_edit("gloss", ch, new, {s.id: s for s in segs})
        spec = e.to_spec()
        self.assertEqual(spec["old"]["uuids"], sorted(s.id for s in segs))
        self.assertEqual(len(spec["old"]["sha256"]), 64)
        self.assertLess(spec["metrics"]["C_new"], spec["metrics"]["C"])
        self.assertGreater(e.gain(), 0)
        other = g.Edit("gloss", "x", "F.Cu", W, [(0, 0), (MM, 0), (MM, MM)], [(0, 0), (MM, MM)])
        far = g.Edit(
            "gloss",
            "y",
            "F.Cu",
            W,
            [(50 * MM, 0), (51 * MM, 0), (51 * MM, MM)],
            [(50 * MM, 0), (51 * MM, MM)],
        )
        same_net = g.Edit(
            "gloss",
            "y",
            "F.Cu",
            W,
            [(80 * MM, 0), (81 * MM, 0), (81 * MM, MM)],
            [(80 * MM, 0), (81 * MM, MM)],
        )
        bs = g.batches([e, other, far, same_net])
        self.assertEqual(sum(len(b) for b in bs), 4)
        for b in bs:
            nets = [n for x in b for n in x.nets]
            self.assertEqual(len(nets), len(set(nets)))
        ops = g.chain_ops(ch, new, {s.id: s for s in segs})
        self.assertEqual(ops["remove"], sorted(s.id for s in segs))
        self.assertEqual(ops["keep"], [])
        self.assertEqual(len(ops["add"]), len(new) - 1)

    def test_chain_ops_keeps_remainders_of_split_segments(self):
        # one long segment carries a T stub at x=4 and a pad-centre projection at x=7
        segs = [seg("long", (0, 0), (10 * MM, 0)), seg("stub", (4 * MM, 0), (4 * MM, 3 * MM))]
        cs = g.build_chains(segs, pin_points=[(7 * MM, 0)])
        mid = [c for c in cs.chains if c.points[0] == (4 * MM, 0) and c.points[-1] == (7 * MM, 0)][
            0
        ]
        ops = g.chain_ops(mid, mid.points, {s.id: s for s in segs})
        self.assertEqual(ops["remove"], ["long"])
        self.assertEqual(
            sorted(ops["keep"]),
            [("long", (0, 0), (4 * MM, 0), W), ("long", (7 * MM, 0), (10 * MM, 0), W)],
        )


# ======================================================================= functional groups (owner decision 2026-09-30)
P_MIN = W + C + g.MARGIN  # minimum pitch of two Default tracks: 0.328 mm


def ct(net, a, b, owner=None, w=W, c=C):
    return g.CTrack(net, tuple(a), tuple(b), w, c, owner or net)


def allowed_fn(tags, cross_mm=g.CROSS_GROUP_MM, budget=None):
    def allowed(a, b):
        mm = g.allowed_parallel_mm(a, b, tags, cross_mm, budget=budget)
        return None if mm is None else mm * MM

    return allowed


def pure_chain_cap(net, old, others, tags, cross_mm=g.CROSS_GROUP_MM):
    """Context.cap for chain `old` of `net` among foreign tracks `others` (CTracks), as Model.chain_cap."""
    layer = g.CouplingLayer([ct(net, a, b, "chain") for a, b in zip(old, old[1:])] + list(others))
    table = g.pair_table(layer)

    def totals(a, b):
        return table.get((a, b) if a < b else (b, a), 0.0)

    edit = g.EditCoupling(layer, {"chain"}, margin=g.TUBE)

    def cap(points):
        delta = edit.delta([ct(net, a, b, "#new") for a, b in zip(points, points[1:]) if a != b])
        bad = g.cap_violations(delta, totals, allowed_fn(tags, cross_mm))
        return "cap:%s|%s" % bad[0][:2] if bad else None

    return cap


def pure_corridor_cap(chains, tags, cross_mm=g.CROSS_GROUP_MM):
    """Corridor.cap over whole-chain owners (a pure stand-in for Model.corridor_cap)."""
    base = {c.id: g.simplify(c.points) for c in chains}
    by = {c.id: c for c in chains}
    layer = g.CouplingLayer(
        [ct(c.net, a, b, c.id) for c in chains for a, b in zip(base[c.id], base[c.id][1:])]
    )
    table = g.pair_table(layer)

    def totals(a, b):
        return table.get((a, b) if a < b else (b, a), 0.0)

    def cap(cid, new, state):
        moved = {c: p for c, p in state.items() if c != cid and list(p) != list(base[c])}
        moved[cid] = new
        add = [
            ct(by[c].net, a, b, "#new")
            for c, pts in sorted(moved.items())
            for a, b in zip(pts, pts[1:])
        ]
        bad = g.cap_violations(
            g.EditCoupling(layer, set(moved), add).delta(), totals, allowed_fn(tags, cross_mm)
        )
        return "cap:%s|%s" % bad[0][:2] if bad else None

    return cap


def run_mm(tracks, a, b):
    return g.pair_table(g.CouplingLayer(tracks)).get((a, b) if a < b else (b, a), 0.0) / MM


class CrossGroupCapTest(unittest.TestCase):
    def test_hook_allowed_parallel_mm(self):
        tags = {"bus_d": "bus", "bus_c": "bus", "ctl": "control"}
        self.assertIsNone(g.allowed_parallel_mm("bus_d", "bus_c", tags))  # one group: unlimited
        self.assertEqual(g.allowed_parallel_mm("bus_d", "ctl", tags), 10.0)  # two groups: 10 mm
        self.assertEqual(
            g.allowed_parallel_mm("bus_d", "loose", tags), 10.0
        )  # grouped vs ungrouped
        self.assertEqual(g.allowed_parallel_mm("loose", "other", tags), 10.0)  # two singletons
        self.assertIsNone(g.allowed_parallel_mm("loose", "loose", tags))
        self.assertEqual(
            g.allowed_parallel_mm("bus_d", "ctl", tags, 4.5), 4.5
        )  # PNR_GLOSS_CROSS_GROUP_MM

        # a noise-budget model: a per-pair allowance replaces the fixed cap between groups
        def budget(a, b):
            return {("bus_d", "ctl"): 2.5}.get(tuple(sorted((a, b))))

        self.assertEqual(g.allowed_parallel_mm("bus_d", "ctl", tags, budget=budget), 2.5)
        self.assertIsNone(g.allowed_parallel_mm("bus_d", "loose", tags, budget=budget))
        self.assertIsNone(
            g.allowed_parallel_mm("bus_d", "bus_c", tags, budget=lambda a, b: 0.0)
        )  # within: unlimited
        self.assertEqual(g.group_of("loose", tags), ("net", "loose"))
        self.assertEqual(g.group_of("bus_d", tags), g.group_of("bus_c", tags))
        self.assertEqual(g.group_of("x", None), ("net", "x"))

    def test_parallel_run_measure(self):
        a = ct("A", (0, 0), (10 * MM, 0))
        for d, want in (
            (P_MIN, 6.0),
            (P_MIN + 100_000, 6.0),
            (P_MIN + 100_100, 0.0),
            (-P_MIN, 6.0),
        ):
            self.assertAlmostEqual(
                run_mm([a, ct("B", (4 * MM, d), (12 * MM, d))], "A", "B"), want, places=6, msg=d
            )
        # 45 deg is no parallel run; 20 deg counts only where the centre distance is within the band
        self.assertEqual(
            run_mm([a, ct("B", (2 * MM, P_MIN), (6 * MM, 4 * MM + P_MIN))], "A", "B"), 0.0
        )
        tilt = ct(
            "B",
            (2 * MM, P_MIN),
            (2 * MM + 3 * MM, P_MIN + round(3 * MM * math.tan(math.radians(20)))),
        )
        self.assertGreater(run_mm([a, tilt], "A", "B"), 0.0)
        self.assertLess(run_mm([a, tilt], "A", "B"), 0.3)
        # independent of how either net is split into segments; same-net copper never counts
        split = [
            ct("B", (4 * MM, P_MIN), (7 * MM, P_MIN), "b1"),
            ct("B", (7 * MM, P_MIN), (12 * MM, P_MIN), "b2"),
        ]
        halves = [ct("A", (0, 0), (5 * MM, 0), "a1"), ct("A", (5 * MM, 0), (10 * MM, 0), "a2")]
        self.assertAlmostEqual(run_mm(halves + split, "A", "B"), 6.0, places=6)
        self.assertEqual(run_mm([a, ct("A", (0, P_MIN), (9 * MM, P_MIN), "a3")], "A", "A"), 0.0)
        # a track between two others runs with each; net_runs agrees with the table; wider tracks
        # have a wider minimum pitch
        x = ct("X", (0, -P_MIN), (3 * MM, -P_MIN))
        layer = g.CouplingLayer([a, x] + split)
        self.assertAlmostEqual(g.pair_table(layer)[("A", "X")] / MM, 3.0, places=6)
        self.assertEqual(
            {k: round(v / MM, 6) for k, v in g.net_runs(layer, "A").items()}, {"B": 6.0, "X": 3.0}
        )
        wide = ct(
            "P", (0, 600_000), (10 * MM, 600_000), w=500_000, c=150_000
        )  # p = 0.35 + 0.151 mm
        self.assertAlmostEqual(run_mm([a, wide], "A", "P"), 10.0, places=6)
        self.assertEqual(
            run_mm([a, ct("P", (0, 602_000), (10 * MM, 602_000), w=500_000, c=150_000)], "A", "P"),
            0.0,
        )

    def test_edit_coupling_delta_equals_recompute(self):
        rng = random.Random(11)
        dirs = [(1, 0), (0, 1), (1, 1), (1, -1)]
        checked = 0
        for trial in range(80):

            def track(net, owner):
                x, y = rng.randrange(0, 12) * 250_000, rng.randrange(0, 10) * 164_000
                d = rng.choice(dirs)
                n = rng.randrange(1, 14) * 250_000
                return ct(
                    net, (x, y), (x + d[0] * n, y + d[1] * n), owner, w=rng.choice((W, W, 500_000))
                )

            tracks = [track("N%d" % rng.randrange(5), "t%d" % k) for k in range(14)]
            layer = g.CouplingLayer(tracks)
            remove = {t.owner for t in rng.sample(tracks, 3)}
            nets = sorted({t.net for t in tracks if t.owner in remove})
            keep = [track(rng.choice(nets), "k%d" % k) for k in range(rng.randrange(0, 2))]
            add = [track(rng.choice(nets), "n%d" % k) for k in range(rng.randrange(1, 4))]
            edit = g.EditCoupling(layer, remove, keep, margin=rng.choice((0, g.TUBE)))
            delta = edit.delta(add)
            before = g.pair_table(layer)
            after = g.pair_table(
                g.CouplingLayer([t for t in tracks if t.owner not in remove] + keep + add)
            )
            for pair in set(before) | set(after) | set(delta):
                want = after.get(pair, 0.0) - before.get(pair, 0.0)
                self.assertAlmostEqual(delta.get(pair, 0.0), want, delta=1e-3, msg=(trial, pair))
                checked += 1
            # the same edit, applied in one go (the corridor's form)
            once = g.EditCoupling(layer, remove, keep + add).delta()
            for pair in set(once) | set(delta):
                self.assertAlmostEqual(once.get(pair, 0.0), delta.get(pair, 0.0), delta=1e-3)
        self.assertGreater(checked, 100)

    def test_cap_violations_and_preexisting_excess(self):
        tags = {"A": "g1", "B": "g1", "X": "g2"}
        before = {("A", "X"): 12 * MM, ("A", "B"): 30 * MM}

        def totals(a, b):
            return before.get((a, b), 0.0)

        allowed = allowed_fn(tags)
        # within a group: unlimited; a pair already over the cap may stay or fall, never rise
        self.assertEqual(g.cap_violations({("A", "B"): 5 * MM}, totals, allowed), [])
        self.assertEqual(g.cap_violations({("A", "X"): -1 * MM}, totals, allowed), [])
        self.assertEqual(g.cap_violations({("A", "X"): 0.0}, totals, allowed), [])
        self.assertEqual(len(g.cap_violations({("A", "X"): 0.5 * MM}, totals, allowed)), 1)
        # under the cap: up to the cap, not beyond
        self.assertEqual(g.cap_violations({("B", "X"): 10 * MM}, totals, allowed), [])
        self.assertEqual(
            g.cap_violations({("B", "X"): 10 * MM + 1000}, totals, allowed)[0][:2], ("B", "X")
        )

    def gloss_case(self, tags, cross_mm=g.CROSS_GROUP_MM):
        # A runs 12 mm at minimum pitch below X (12 > 10: over the cap before the edit), then dips
        # away for 2 mm; pulling the dip tight would extend the run to 14 mm
        old = [(0, 0), (12 * MM, 0), (12_500_000, -500_000), (13_500_000, -500_000), (14 * MM, 0)]
        x = ct("X", (-MM, P_MIN), (15 * MM, P_MIN))
        w = World().track("X", x.a, x.b)
        cap = pure_chain_cap("A", old, [x], tags, cross_mm) if tags is not None else None
        ctx = w.ctx("A", cap=cap)
        return old, x, ctx, g.gloss(old, ctx)

    def test_gloss_never_raises_a_preexisting_excess(self):
        old, x, ctx, free = self.gloss_case(None)
        self.assertEqual(free, [(0, 0), (14 * MM, 0)])  # no groups file: pulled tight
        self.assertAlmostEqual(
            run_mm([ct("A", p, q) for p, q in zip(free, free[1:])] + [x], "A", "X"), 14.0, places=3
        )
        _, _, ctx, same = self.gloss_case({"A": "bus", "X": "bus"})
        self.assertEqual(same, free)  # one group: unlimited
        _, _, ctx, capped = self.gloss_case({"A": "bus", "X": "ctl"})
        before = run_mm([ct("A", p, q) for p, q in zip(old, old[1:])] + [x], "A", "X")
        self.assertAlmostEqual(before, 12.0, places=3)
        if capped is not None:
            self.assertLessEqual(
                run_mm([ct("A", p, q) for p, q in zip(capped, capped[1:])] + [x], "A", "X"),
                before + 1e-6,
            )
        self.assertGreater(ctx.calls["cap_refused"], 0)
        self.assertEqual(g.check_edit(old, free, ctx), "cap:A|X")
        # a 20 mm cap admits the 14 mm run
        _, _, _, wide = self.gloss_case({"A": "bus", "X": "ctl"}, cross_mm=20.0)
        self.assertEqual(wide, free)

    def test_corridor_packs_within_group_and_caps_across(self):
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 16 * MM)  # 14 mm legs, 0.8 mm edge gap
        b = pi_chain("b", "B", 6 * MM, MM, 17 * MM)
        (free,) = corridor([a, b]).plan()
        same = corridor([a, b], cap=pure_corridor_cap([a, b], {"A": "bus", "B": "bus"})).plan()
        self.assertEqual([e.to_spec() for e in same], [free.to_spec()])  # within a group: unlimited
        after = apply([a, b], free)
        tracks = [ct(c.net, p, q, c.id) for c in after for p, q in zip(c.points, c.points[1:])]
        self.assertAlmostEqual(run_mm(tracks, "A", "B"), 14.0, delta=0.01)
        for tags in ({"A": "bus", "B": "ctl"}, {"A": "bus"}, {}):  # other group / ungrouped
            cor = corridor([a, b], cap=pure_corridor_cap([a, b], tags))
            (stack,) = horizontal(cor.stacks())
            _, moves, reasons = cor._pack(stack, 0, {x.id: g.simplify(x.points) for x in (a, b)})
            self.assertEqual(
                (moves, {m.net: r for m, r in reasons.items()}), ({}, {"B": "cap"}), tags
            )
            # what the planner may still do (A's partial lift, bounded by the 0.2 mm slack) stays
            # off minimum pitch
            for e in cor.plan():
                after = apply([a, b], e)
                tracks = [
                    ct(x.net, p, q, x.id) for x in after for p, q in zip(x.points, x.points[1:])
                ]
                self.assertLessEqual(run_mm(tracks, "A", "B"), 10.0, tags)
            self.assertGreater(cor.stats["cap_refused"], 0)
        wide = corridor(
            [a, b], cap=pure_corridor_cap([a, b], {"A": "bus", "B": "ctl"}, cross_mm=15.0)
        ).plan()
        self.assertEqual([e.to_spec() for e in wide], [free.to_spec()])
        # short legs (6 mm) stay under the cap: packed across groups too
        a6, b6 = pi_chain("a", "A", 5 * MM, 2 * MM, 8 * MM), pi_chain("b", "B", 6 * MM, MM, 9 * MM)
        self.assertEqual(
            len(corridor([a6, b6], cap=pure_corridor_cap([a6, b6], {"A": "x", "B": "y"})).plan()), 1
        )

    def test_corridor_cap_stops_a_member_and_the_rest_pack_against_it(self):
        # three stacked 14 mm legs: A and B one group, C another. Packing toward A moves B (same group,
        # unlimited) and would put C at minimum pitch beside B for 14 mm: C stays
        a = pi_chain("a", "A", 5 * MM, 2 * MM, 16 * MM)
        b = pi_chain("b", "B", 6 * MM, MM, 17 * MM)
        c = pi_chain("c", "C", 7 * MM, 0, 18 * MM)
        cor = corridor(
            [a, b, c], cap=pure_corridor_cap([a, b, c], {"A": "bus", "B": "bus", "C": "ctl"})
        )
        (stack,) = [st for st in horizontal(cor.stacks()) if len(st) == 3]
        base = {x.id: g.simplify(x.points) for x in (a, b, c)}
        state, moves, reasons = cor._pack(stack, 0, base)
        self.assertEqual(sorted(m.net for m in moves), ["B"])
        self.assertEqual({m.net: r for m, r in reasons.items()}, {"C": "cap"})
        self.assertEqual(state["b"][1][1], 5 * MM + P_MIN)
        self.assertEqual(state["c"], base["c"])
        tracks = [
            ct(x.net, p, q, x.id) for x in (a, b, c) for p, q in zip(state[x.id], state[x.id][1:])
        ]
        self.assertGreater(run_mm(tracks, "A", "B"), 10.0)  # one group: 14 mm
        self.assertLessEqual(run_mm(tracks, "B", "C"), 10.0)
        for e in cor.plan():  # whatever the planner picks
            after = apply([a, b, c], e)
            tracks = [ct(x.net, p, q, x.id) for x in after for p, q in zip(x.points, x.points[1:])]
            self.assertLessEqual(run_mm(tracks, "B", "C"), 10.0)
            self.assertLessEqual(run_mm(tracks, "A", "C"), 10.0)

    def test_dekink_hug_capped_falls_back_to_own_group(self):
        # test_dekink_snaps_to_same_class_neighbour with a 1 mm cap: hugging X (another group) for
        # ~3 mm is refused; the DP falls back to hugging its own group (none here), then to none
        old = [
            (0, 0),
            (2 * MM, 0),
            (3 * MM, -MM),
            (6_750_000, -MM),
            (7_750_000, -2 * MM),
            (9 * MM, -2 * MM),
        ]
        legs = {(0, 0): [(-MM, MM)], (9 * MM, -2 * MM): [(MM, -MM)]}
        neighbour = ("X", (3_500_000, -1_600_000), (6_500_000, -1_600_000))
        w = World().track("X", neighbour[1], neighbour[2])
        index, hug, snap = ray_world([neighbour])
        plain = g.dekink(old, w.ctx("n", anchor_legs=legs))
        hugged = g.dekink(old, w.ctx("n", anchor_legs=legs, hug=hug, snap=snap, clearance=C))
        x = [ct("X", neighbour[1], neighbour[2])]
        self.assertGreater(
            run_mm([ct("n", p, q) for p, q in zip(hugged, hugged[1:])] + x, "n", "X"), 1.0
        )

        def ctx(tags, cross_mm):
            def accept(it):
                return g.group_of(it.net, tags) == g.group_of("n", tags)

            def hug_group(a, b):
                return g.hug_segment(a, b, W / 2, "n", K_DEF, C, index, accept=accept)

            def snap_group(box):
                return [r for r in snap(box) if accept(r)]

            return w.ctx(
                "n",
                anchor_legs=legs,
                hug=hug,
                snap=snap,
                clearance=C,
                hug_group=hug_group,
                snap_group=snap_group,
                cap=pure_chain_cap("n", old, x, tags, cross_mm),
            )

        self.assertEqual(
            g.dekink(old, ctx({"n": "bus", "X": "bus"}, 1.0)), hugged
        )  # one group: unlimited
        self.assertEqual(
            g.dekink(old, ctx({"n": "bus", "X": "ctl"}, 10.0)), hugged
        )  # within the cap
        c = ctx({"n": "bus", "X": "ctl"}, 1.0)
        capped = g.dekink(old, c)
        self.assertGreater(c.calls["cap_refused"], 0)
        self.assertEqual(capped, plain)
        self.assertEqual(c.level, "all")  # level restored
        self.assertLessEqual(
            run_mm([ct("n", p, q) for p, q in zip(capped, capped[1:])] + x, "n", "X"), 1.0
        )


if __name__ == "__main__":
    unittest.main()
