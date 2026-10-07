"""Bucket indexes over the detailed grid's obstacle lists.

The exact clearance checks (:func:`.joint_escape._segment_clear`,
:func:`.joint_escape._grazes_own_lands`) judge a candidate segment against the
grid's pad rectangles, escape copper and surface lands. Scanned whole, each
check runs the segment-distance test (:func:`pnr.writeback._segment_distance_sq`)
against every item of a list of hundreds, almost all of them millimetres away.

The lists are :class:`TrackedList` (``RouteGrid`` creates them so): a list that
counts its structural changes. :func:`near` keeps one uniform bucket grid per
list (``BUCKET_MM``) in step with it, adding appended items as they come and
rebuilding after any other change, and answers a query box with the positions,
in list order, of every item whose box meets the query box's buckets. A caller
widens its query box by the largest reach any item could be judged at (plus
``SLACK_MM``), so every item it leaves out fails the exact test's own bounding
box test, or lies farther than any limit the test accepts: the check returns
the same answer, from the same arithmetic on the same items, as the full scan.
A plain list (a test that assigns one) has no index: :func:`near` returns None
and the caller scans it whole.
"""

from __future__ import annotations

import math

BUCKET_MM = 1.0
# Added to every query reach. Far above the rounding of the exact tests (board
# coordinates of a few hundred mm carry about 1e-13 mm), so an item outside the query
# box is farther than any limit the exact test could pass or fail it at by rounding.
SLACK_MM = 1e-6


class TrackedList(list):
    """A list that records structural changes for :func:`near`: ``version`` counts
    every change other than adding at the end (``append``, ``extend``, ``+=``),
    which an index picks up from the length alone."""

    __slots__ = ("version", "indexes")

    def __init__(self, *args):
        super().__init__(*args)
        self.version = 0
        self.indexes = {}

    def __reduce_ex__(self, protocol):
        # Copies and pickles (spawned route workers) carry the items, not the indexes.
        return (type(self), (list(self),))

    def _changed(self):
        self.version += 1

    def __setitem__(self, key, value):
        self._changed()
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._changed()
        super().__delitem__(key)

    def __imul__(self, n):
        self._changed()
        return super().__imul__(n)

    def insert(self, index, value):
        self._changed()
        super().insert(index, value)

    def remove(self, value):
        self._changed()
        super().remove(value)

    def pop(self, *args):
        self._changed()
        return super().pop(*args)

    def clear(self):
        self._changed()
        super().clear()

    def sort(self, *args, **kwargs):
        self._changed()
        super().sort(*args, **kwargs)

    def reverse(self):
        self._changed()
        super().reverse()


def _rect_item(item):
    # pad_rectangles (layer, net, Rect) and smd_pads (layer, net, Rect, corner).
    r = item[2]
    return item[0], r.left, r.bottom, r.right, r.top


def _segment_item(item):
    # escape_segments (layer, net, a, b).
    _, _, a, b = item
    return item[0], min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def _via_item(item):
    # escape_vias (net, point): every layer.
    p = item[1]
    return None, p[0], p[1], p[0], p[1]


SHAPES = {"rect": _rect_item, "segment": _segment_item, "via": _via_item}


class _Index:
    __slots__ = ("shape", "size", "version", "count", "buckets")

    def __init__(self, shape):
        self.shape = shape
        self.size = BUCKET_MM
        self.version = None
        self.count = 0
        self.buckets = {}

    def sync(self, items):
        if self.version != items.version or self.count > len(items):
            self.version = items.version
            self.count = 0
            self.buckets = {}
        add = self.buckets.setdefault
        size = self.size
        for position in range(self.count, len(items)):
            layer, x0, y0, x1, y1 = self.shape(items[position])
            for i in range(math.floor(x0 / size), math.floor(x1 / size) + 1):
                for j in range(math.floor(y0 / size), math.floor(y1 / size) + 1):
                    add((layer, i, j), []).append(position)
        self.count = len(items)

    def query(self, layer, x0, y0, x1, y1):
        get = self.buckets.get
        size = self.size
        found = set()
        for i in range(math.floor(x0 / size), math.floor(x1 / size) + 1):
            for j in range(math.floor(y0 / size), math.floor(y1 / size) + 1):
                hit = get((layer, i, j))
                if hit:
                    found.update(hit)
        return sorted(found)


def near(items, shape, layer, a, b, reach):
    """Positions (ascending) of the items of ``items`` (a :class:`TrackedList` of
    ``shape`` items: "rect", "segment" or "via") on ``layer`` (ignored for vias,
    which are on every layer) whose box may come within ``reach`` of the box of
    segment ``a``-``b``; every other item's box is farther than ``reach`` +
    ``SLACK_MM`` from it on some axis. None for a plain list: scan it whole."""
    if type(items) is not TrackedList:
        return None
    index = items.indexes.get(shape)
    if index is None:
        index = items.indexes[shape] = _Index(SHAPES[shape])
    index.sync(items)
    grow = reach + SLACK_MM
    return index.query(
        None if shape == "via" else layer,
        min(a[0], b[0]) - grow,
        min(a[1], b[1]) - grow,
        max(a[0], b[0]) + grow,
        max(a[1], b[1]) + grow,
    )
