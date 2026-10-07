"""Dynamic layer-aware uniform-grid broadphase; candidates need exact checks.

No tree rebuild occurs after a local move. Very large records use per-layer
oversize lists; very large queries deliberately fall back to a layer scan.
These bounded fallbacks preserve completeness but can cost O(N).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    layer: str
    bounds: tuple[float, float, float, float]


def expand(bounds, margin):
    a, b, c, d = bounds
    return (a - margin, b - margin, c + margin, d + margin)


def overlaps(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


class LayerGrid:
    def __init__(self, cell_mm=2.0, max_cells=4096):
        if not math.isfinite(cell_mm) or cell_mm <= 0 or max_cells < 1:
            raise ValueError("Finite positive cell size and positive cell budget required")
        self.cell_mm = cell_mm
        self.max_cells = max_cells
        self.records = {}
        self.buckets = defaultdict(set)
        self.memberships = {}
        self.layers = defaultdict(set)
        self.oversize = defaultdict(set)
        self.stats = dict(
            queries=0,
            bucket_visits=0,
            candidate_ids_examined=0,
            fullscan_queries=0,
            updates=0,
            rebuilds=0,
        )

    def _cell_range(self, bounds):
        if (
            len(bounds) != 4
            or any(not math.isfinite(x) for x in bounds)
            or bounds[0] > bounds[2]
            or bounds[1] > bounds[3]
        ):
            raise ValueError("Finite ordered bounds required")
        a, b, c, d = bounds
        s = self.cell_mm
        # Inclusive boundary cells, with a floating-point guard independent of
        # width/clearance. Geometry-level tests remain authoritative.
        x0, x1 = math.floor((a - 1e-8) / s), math.floor((c + 1e-8) / s)
        y0, y1 = math.floor((b - 1e-8) / s), math.floor((d + 1e-8) / s)
        count = (x1 - x0 + 1) * (y1 - y0 + 1)
        return (x0, y0, x1, y1, count)

    def upsert(self, id, layer, bounds):
        r = self._cell_range(bounds)
        self.remove(id)
        self.records[id] = Record(layer, tuple(bounds))
        self.layers[layer].add(id)
        if r[4] > self.max_cells:
            self.memberships[id] = None
            self.oversize[layer].add(id)
        else:
            cells = tuple(
                (layer, x, y) for x in range(r[0], r[2] + 1) for y in range(r[1], r[3] + 1)
            )
            self.memberships[id] = cells
            for key in cells:
                self.buckets[key].add(id)
        self.stats["updates"] += 1

    def remove(self, id):
        record = self.records.pop(id, None)
        if record is None:
            return
        self.layers[record.layer].discard(id)
        self.oversize[record.layer].discard(id)
        for key in self.memberships.pop(id) or ():
            self.buckets[key].discard(id)
            if not self.buckets[key]:
                del self.buckets[key]

    def query(self, bounds, layers=None):
        r = self._cell_range(bounds)
        self.stats["queries"] += 1
        chosen = sorted(self.layers if layers is None else set(layers))
        candidates = set()
        for layer in chosen:
            if r[4] > self.max_cells:
                candidates.update(self.layers.get(layer, ()))
                self.stats["fullscan_queries"] += 1
            else:
                candidates.update(self.oversize.get(layer, ()))
                for x in range(r[0], r[2] + 1):
                    for y in range(r[1], r[3] + 1):
                        self.stats["bucket_visits"] += 1
                        candidates.update(self.buckets.get((layer, x, y), ()))
        self.stats["candidate_ids_examined"] += len(candidates)
        return [id for id in sorted(candidates) if overlaps(bounds, self.records[id].bounds)]
