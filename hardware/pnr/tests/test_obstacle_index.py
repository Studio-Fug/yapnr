"""The obstacle bucket indexes (pnr.route.detail.obstacle_index) and the written-out
segment distance (pnr.writeback._segment_distance_sq) change no clearance answer.

The exact checks of joint_escape run on a real board's grid (the splanc_dev fixture,
as route_board builds it) once through the indexes and once over the plain lists
(the full scans they replace), and must agree on every query."""

import copy
import math
import os
import pickle
import random
import struct
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pnr.route.detail import joint_escape, obstacle_index  # noqa: E402
from pnr.route.detail.obstacle_index import TrackedList, near  # noqa: E402
from pnr.writeback import _point_dist_sq, _segment_distance_sq  # noqa: E402

LISTS = ("pad_rectangles", "escape_segments", "escape_vias", "smd_pads")


@contextmanager
def full_scan(grid):
    """``grid`` with plain lists: no index, every check scans the whole list."""
    saved = {name: getattr(grid, name) for name in LISTS}
    for name, items in saved.items():
        setattr(grid, name, list(items))
    try:
        yield grid
    finally:
        for name, items in saved.items():
            setattr(grid, name, items)


def _box_gap(box, a, b):
    x0, y0, x1, y1 = box
    dx = max(0.0, x0 - max(a[0], b[0]), min(a[0], b[0]) - x1)
    dy = max(0.0, y0 - max(a[1], b[1]), min(a[1], b[1]) - y1)
    return max(dx, dy)


class TrackedListTest(unittest.TestCase):
    def test_changes_other_than_appends_count(self):
        items = TrackedList([3, 1, 2])
        for change in (
            lambda x: x.append(4),
            lambda x: x.extend([5, 6]),
            lambda x: x.__iadd__([7]),
        ):
            change(items)
            self.assertEqual(items.version, 0)
        for change in (
            lambda x: x.insert(0, 9),
            lambda x: x.remove(9),
            lambda x: x.pop(),
            lambda x: x.__setitem__(0, 8),
            lambda x: x.__setitem__(slice(None), [1, 2, 3]),
            lambda x: x.__delitem__(0),
            lambda x: x.sort(),
            lambda x: x.reverse(),
            lambda x: x.__imul__(2),
            lambda x: x.clear(),
        ):
            before = items.version
            change(items)
            self.assertEqual(items.version, before + 1)

    def test_copies_carry_items_not_indexes(self):
        items = TrackedList([(0, "A", (0.0, 0.0), (1.0, 1.0))])
        near(items, "segment", 0, (0.0, 0.0), (0.0, 0.0), 0.1)
        self.assertTrue(items.indexes)
        for other in (pickle.loads(pickle.dumps(items)), copy.deepcopy(items), copy.copy(items)):
            self.assertIs(type(other), TrackedList)
            self.assertEqual(other, items)
            self.assertEqual((other.version, other.indexes), (0, {}))

    def test_plain_list_has_no_index(self):
        self.assertIsNone(near([(0, "A", (0.0, 0.0), (1.0, 1.0))], "segment", 0, (0, 0), (0, 0), 1))

    def test_index_keeps_up_with_every_change(self):
        """After any sequence of changes the index returns, in list order, a superset
        of the items whose box comes within the reach (and only valid positions)."""
        rng = random.Random(5)

        def segment():
            a = (rng.uniform(-3, 12), rng.uniform(-3, 12))
            if rng.random() < 0.3:
                return (rng.randrange(2), "N%d" % rng.randrange(4), a, a)
            b = (a[0] + rng.uniform(-4, 4), a[1] + rng.uniform(-4, 4))
            return (rng.randrange(2), "N%d" % rng.randrange(4), a, b)

        items = TrackedList(segment() for _ in range(20))
        changes = [
            lambda: items.append(segment()),
            lambda: items.extend(segment() for _ in range(rng.randrange(4))),
            lambda: items.pop(rng.randrange(len(items))) if items else None,
            lambda: items.remove(rng.choice(items)) if items else None,
            lambda: items.insert(rng.randrange(len(items) + 1), segment()),
            lambda: items.__setitem__(slice(None), [s for s in items if s[1] != "N1"]),
            lambda: items.__setitem__(rng.randrange(len(items)), segment()) if items else None,
            lambda: items.sort(),
        ]
        for _ in range(400):
            rng.choice(changes)()
            for _ in range(5):
                la = rng.randrange(2)
                a = (rng.uniform(-3, 12), rng.uniform(-3, 12))
                b = (a[0] + rng.uniform(-2, 2), a[1] + rng.uniform(-2, 2))
                reach = rng.uniform(0, 1.5)
                hits = near(items, "segment", la, a, b, reach)
                self.assertEqual(hits, sorted(set(hits)))
                self.assertTrue(all(0 <= k < len(items) for k in hits))
                for k, (layer, _, c, d) in enumerate(items):
                    box = (min(c[0], d[0]), min(c[1], d[1]), max(c[0], d[0]), max(c[1], d[1]))
                    if layer == la and _box_gap(box, a, b) <= reach:
                        self.assertIn(k, hits)


class RealBoardTest(unittest.TestCase):
    """joint_escape's exact checks on the splanc_dev grid: indexed == full scan."""

    @classmethod
    def setUpClass(cls):
        from test_dense_maze import fixture_grid

        cls.grid, _ = fixture_grid()
        cls.nets = sorted({owner for _, owner, _ in cls.grid.pad_rectangles}) + ["N/A"]

    def queries(self, rng, count, anchors=None, spread=1.5, length=2.5):
        """Segments around ``anchors`` (by default pads, escape copper ends and escape
        vias), where clearance limits are met or just missed."""
        grid = self.grid
        if anchors is None:
            anchors = [(r.cx, r.cy) for _, _, r in grid.pad_rectangles]
            anchors += [p for _, _, a, b in grid.escape_segments for p in (a, b)]
            anchors += [p for _, p in grid.escape_vias]
        # On the board (the fixture leaves some parts beside it): off it, every
        # segment fails the bounds test before any obstacle is judged.
        anchors = [(x, y) for x, y in anchors if 1 < x < grid.width - 1 and 1 < y < grid.height - 1]
        nets = self.nets
        for _ in range(count):
            x, y = rng.choice(anchors)
            a = (x + rng.uniform(-spread, spread), y + rng.uniform(-spread, spread))
            if rng.random() < 0.2:
                b = a
            else:
                b = (a[0] + rng.uniform(-length, length), a[1] + rng.uniform(-length, length))
            yield (
                rng.choice(nets),
                rng.randrange(grid.nlayers),
                a,
                b,
                rng.choice((grid.track_width, grid.track_width * 1.7, 0.6)),
            )

    def check(self, grid, rng, count=2000, queries=None, options=({}, dict(offsets=False))):
        outcomes = set()
        for net, layer, a, b, width in queries or self.queries(rng, count):
            for kwargs in options:
                fast = joint_escape._segment_clear(grid, net, layer, a, b, width, **kwargs)
                with full_scan(grid):
                    slow = joint_escape._segment_clear(grid, net, layer, a, b, width, **kwargs)
                self.assertEqual(fast, slow, (net, layer, a, b, width, kwargs))
                outcomes.add(fast)
        self.assertEqual(outcomes, {True, False})

    def test_segment_clear_matches_full_scan(self):
        self.check(
            self.grid, random.Random(1), options=({}, dict(offsets=False), dict(net_keepouts=False))
        )

    @contextmanager
    def exposed(self, *kept):
        """Only the obstacle lists ``kept``, buckets far smaller than the clearances
        (the index returns little more than its reach) and no blocked mask (the exact
        tests decide every answer): a reach too short changes answers here."""
        grid = self.grid
        saved = {name: getattr(grid, name) for name in LISTS}
        try:
            for name in LISTS:
                setattr(grid, name, TrackedList(saved[name] if name in kept else ()))
            with patch.object(obstacle_index, "BUCKET_MM", 0.05), patch.object(
                grid, "blocked", np.zeros_like(grid.blocked)
            ):
                yield grid
        finally:
            for name, items in saved.items():
                setattr(grid, name, items)

    def corners(self, rng):
        return [
            (rng.choice((r.left, r.right)), rng.choice((r.bottom, r.top)))
            for _, _, r in self.grid.pad_rectangles
        ]

    def test_pads_alone_with_fine_buckets(self):
        rng = random.Random(4)
        anchors = self.corners(rng)
        with self.exposed("pad_rectangles") as grid:
            self.check(grid, rng, queries=list(self.queries(rng, 1500, anchors, 0.6, 0.8)))

    def test_pads_with_keepaways_and_classes_with_fine_buckets(self):
        rng = random.Random(7)
        grid = self.grid
        nets = sorted({owner for _, owner, _ in grid.pad_rectangles if owner})
        keyed = rng.sample(list(grid.pad_rectangles), len(grid.pad_rectangles) // 2)
        anchors = [
            (rng.choice((r.left, r.right)), rng.choice((r.bottom, r.top))) for *_, r in keyed
        ]
        classes = {net: rng.uniform(0.15, 0.4) for net in rng.sample(nets, len(nets) // 3)}
        keepaways = {key: rng.uniform(0.2, 0.6) for key in keyed}
        with patch.object(grid, "pad_keepaways", keepaways):
            with self.exposed("pad_rectangles"):
                self.check(grid, rng, queries=list(self.queries(rng, 1000, anchors, 0.8, 0.8)))
            with patch.object(grid, "net_clearances", classes), self.exposed("pad_rectangles"):
                self.check(grid, rng, queries=list(self.queries(rng, 1000, anchors, 0.8, 0.8)))

    def test_escape_copper_alone_with_fine_buckets(self):
        rng = random.Random(6)
        pads = [(r.cx, r.cy) for _, _, r in self.grid.pad_rectangles]
        with self.exposed("escape_segments") as grid:
            for net, layer, a, b, _ in self.queries(rng, 60, pads):
                grid.escape_segments.append((layer, net, a, b))
            anchors = [p for _, _, a, b in grid.escape_segments for p in (a, b)]
            self.check(grid, rng, queries=list(self.queries(rng, 1500, anchors, 0.6, 0.8)))
        with self.exposed("escape_vias") as grid:
            for net, _, a, _, _ in self.queries(rng, 60, pads):
                grid.escape_vias.append((net, a))
            anchors = [p for _, p in grid.escape_vias]
            self.check(grid, rng, queries=list(self.queries(rng, 1500, anchors, 0.6, 0.8)))

    def test_segment_clear_with_escape_copper_classes_and_keepaways(self):
        """More escape copper (appended: the index adds it), net class clearances and
        pad keepaways (both widen the reach the index must cover)."""
        grid = self.grid
        rng = random.Random(2)
        saved = (
            list(grid.escape_segments),
            list(grid.escape_vias),
            dict(grid.net_clearances),
            dict(grid.pad_keepaways),
        )
        try:
            nets = sorted({owner for _, owner, _ in grid.pad_rectangles if owner})
            for _ in range(3):
                for net, layer, a, b, _ in self.queries(rng, 40):
                    grid.escape_segments.append((layer, net, a, b))
                for net, _, a, _, _ in self.queries(rng, 15):
                    grid.escape_vias.append((net, a))
                for net in rng.sample(nets, 5):
                    grid.net_clearances[net] = rng.uniform(0.2, 0.5)
                for key in rng.sample(list(grid.pad_rectangles), 40):
                    grid.pad_keepaways[key] = rng.uniform(0.2, 0.6)
                self.check(grid, rng, 600)
                for kept in (("pad_rectangles",), ("escape_segments", "escape_vias")):
                    with self.exposed(*kept):
                        anchors = [(r.cx, r.cy) for _, _, r in grid.pad_rectangles]
                        anchors += [p for _, _, a, b in grid.escape_segments for p in (a, b)]
                        anchors += [p for _, p in grid.escape_vias]
                        self.check(
                            grid, rng, queries=list(self.queries(rng, 500, anchors, 0.8, 0.8))
                        )
            del grid.escape_segments[: len(grid.escape_segments) // 2]  # a rebuild
            self.check(grid, rng, 600)
        finally:
            grid.escape_segments[:] = saved[0]
            grid.escape_vias[:] = saved[1]
            grid.net_clearances = saved[2]
            grid.pad_keepaways = saved[3]

    def test_grazes_own_lands_matches_full_scan(self):
        self.check_lands(random.Random(3))

    def test_grazes_own_lands_matches_full_scan_with_fine_buckets(self):
        with self.exposed("smd_pads"):
            self.check_lands(random.Random(5))

    def check_lands(self, rng):
        grid = self.grid
        outcomes = set()
        pads = list(grid.smd_pads)
        lands = {}
        for la, owner, r, _ in pads:
            lands.setdefault((la, owner), []).append(r)
        on_board = [p for p in pads if 0 < p[2].cx < grid.width and 0 < p[2].cy < grid.height]
        for _ in range(2000):
            layer, net, rect, _ = rng.choice(on_board)
            start = (rect.cx, rect.cy)
            segments = []
            for _ in range(rng.randrange(1, 4)):
                end = (start[0] + rng.uniform(-2, 2), start[1] + rng.uniform(-2, 2))
                others = [r for r in lands[(layer, net)] if r != rect]
                if others and rng.random() < 0.6:  # towards the edge of another land
                    r = rng.choice(others)
                    end = (
                        rng.choice((r.left, r.right, r.cx)) + rng.uniform(-0.35, 0.35),
                        rng.choice((r.bottom, r.top, r.cy)) + rng.uniform(-0.35, 0.35),
                    )
                la = layer if rng.random() < 0.85 else 1 - layer
                segments.append((la, start, end, rng.choice((0.15, 0.25, 0.5))))
                start = end
            fast = joint_escape._grazes_own_lands(grid, net, layer, segments, rect)
            with full_scan(grid):
                slow = joint_escape._grazes_own_lands(grid, net, layer, segments, rect)
            self.assertEqual(fast, slow, (net, layer, segments, rect))
            outcomes.add(fast)
        self.assertEqual(outcomes, {True, False})


def _cross_reference(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _point_reference(p, u, v):
    """pnr.writeback._point_dist_sq before it was written out (min/max builtins)."""
    dx, dy = v[0] - u[0], v[1] - u[1]
    den = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - u[0]) * dx + (p[1] - u[1]) * dy) / den)) if den else 0.0
    return (p[0] - u[0] - t * dx) ** 2 + (p[1] - u[1] - t * dy) ** 2


def _segment_reference(a, b, c, d):
    """pnr.writeback._segment_distance_sq before it was written out."""
    if (
        _cross_reference(a, b, c) * _cross_reference(a, b, d) < 0
        and _cross_reference(c, d, a) * _cross_reference(c, d, b) < 0
    ):
        return 0.0
    return min(
        _point_reference(a, c, d),
        _point_reference(b, c, d),
        _point_reference(c, a, b),
        _point_reference(d, a, b),
    )


def _outcome(function, *args):
    """The result, or the type of the exception raised (``** 2`` of a huge double
    raises OverflowError, as it did before)."""
    try:
        return function(*args)
    except ArithmeticError as error:
        return type(error)


def _same(x, y):
    """The same value and type, bit for bit (NaN equals NaN, -0.0 is not 0.0)."""
    if type(x) is not type(y):
        return False
    if isinstance(x, float):
        return struct.pack("<d", x) == struct.pack("<d", y)
    return x == y


class DistanceTest(unittest.TestCase):
    """The written-out distance gives the old function's result bit for bit."""

    def points(self, rng):
        special = [0.0, -0.0, 1.0, -1.0, 1e-300, 5e-324, 1e300, math.inf, -math.inf, math.nan]
        kind = rng.randrange(6)
        if kind == 0:  # board coordinates, mm
            return (rng.uniform(-50, 250), rng.uniform(-50, 250))
        if kind == 1:  # integer nanometres (KiCad side)
            return (rng.randrange(-(10**8), 10**9), rng.randrange(-(10**8), 10**9))
        if kind == 2:  # a coarse lattice: shared points, collinear and touching segments
            return (rng.randrange(-3, 4) * 0.5, rng.randrange(-3, 4) * 0.5)
        if kind == 3:
            return (rng.choice(special), rng.choice(special))
        if kind == 4:  # nearly equal to a lattice point
            return (rng.randrange(3) + rng.choice((0.0, 1e-12, -1e-12)), rng.randrange(3) * 1.0)
        return (rng.uniform(-1e-9, 1e-9), rng.uniform(-1e-9, 1e-9))

    def test_segment_distance_is_bit_identical(self):
        rng = random.Random(11)
        for _ in range(200000):
            a, b, c, d = (self.points(rng) for _ in range(4))
            if rng.random() < 0.15:
                b = a
            if rng.random() < 0.15:
                d = c
            got = _outcome(_segment_distance_sq, a, b, c, d)
            want = _outcome(_segment_reference, a, b, c, d)
            self.assertTrue(_same(got, want), (a, b, c, d, got, want))

    def test_point_distance_is_bit_identical(self):
        rng = random.Random(12)
        for _ in range(200000):
            p, u, v = (self.points(rng) for _ in range(3))
            if rng.random() < 0.15:
                v = u
            got, want = _outcome(_point_dist_sq, p, u, v), _outcome(_point_reference, p, u, v)
            self.assertTrue(_same(got, want), (p, u, v, got, want))


if __name__ == "__main__":
    unittest.main()
