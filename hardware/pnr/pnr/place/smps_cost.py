"""Constraint-derived SMPS placement debt, opt-in with PNR_SMPS_PLACEMENT.

Terminal limits and loop perimeter guide placement; routing and the independent
SMPS audit remain authoritative. For area-only checks an area budget supplies a
conservative perimeter target by the isoperimetric inequality. The independent
audit still measures actual area.
"""

import math
import os

WEIGHT = 100.0


def enabled():
    return os.environ.get("PNR_SMPS_PLACEMENT") == "1"


class Cost:
    def __init__(self, graph, constraints):
        self.keys = {}
        self.owners = []
        for c in graph.components:
            for p in c.pads:
                self.keys.setdefault(c.ref + ":" + p.name, []).append(len(self.owners))
                self.owners.append(c.ref)
        self.rows = []
        for check in constraints.smps_checks:
            kind = check["kind"]
            if kind not in ("pad_distance", "loop_geometry", "route_path"):
                continue
            terms = check["terminals"]
            if any(t not in self.keys for t in terms):
                raise ValueError("SMPS placement terminal missing: " + check["id"])
            limit = check.get("max_mm" if kind == "pad_distance" else "max_length_mm")
            if kind == "loop_geometry":
                limits = []
                if check.get("max_perimeter_mm") is not None:
                    limits.append(check["max_perimeter_mm"])
                if check.get("max_area_mm2") is not None:
                    limits.append(math.sqrt(4 * math.pi * check["max_area_mm2"]))
                limit = limits[0] if limits else None
            if limit is None:
                continue
            self.rows.append(
                dict(
                    id=check["id"],
                    kind=kind,
                    indices=[self.keys[t] for t in terms],
                    limit=limit,
                    refs=sorted({t.rsplit(":", 1)[0] for t in terms}),
                )
            )

    def terms(self, xy, *, torch_mode=False):
        if torch_mode:
            import torch as xp
        else:
            import numpy as xp
        for row in self.rows:
            if row["kind"] == "loop_geometry":
                points = [xy[..., ids, :].mean(-2) for ids in row["indices"]]
                distances = [
                    xp.sqrt(((a - b) ** 2).sum(-1) + 1e-12)
                    for a, b in zip(points, points[1:] + points[:1])
                ]
                value = sum(distances)
            else:
                a, b = [xy[..., ids, :] for ids in row["indices"]]
                distances = xp.sqrt(
                    ((a[..., :, None, :] - b[..., None, :, :]) ** 2).sum(-1) + 1e-12
                )
                if torch_mode:
                    value = distances.flatten(-2).min(-1).values
                else:
                    value = distances.reshape(distances.shape[:-2] + (-1,)).min(-1)
            debt = (value - row["limit"]) / max(row["limit"], 1.0)
            debt = debt.clamp(min=0) if torch_mode else xp.maximum(debt, 0)
            yield row, debt**2

    def torch(self, xy):
        return sum((v for _, v in self.terms(xy, torch_mode=True)), xy.new_zeros(xy.shape[:-2]))


class Slots:
    """Score a proposed pose only against complete, already placed obligations."""

    def __init__(self, graph, constraints, previous=None):
        self.cost = Cost(graph, constraints)
        self.previous = previous
        self.refs = {ref for row in self.cost.rows for ref in row["refs"]}
        self.refs.update(getattr(previous, "refs", set()))
        self.graph = graph

    def __call__(self, comp, neighbors, xs, ys):
        import numpy as np

        from .geometry import pin_positions

        shape = np.broadcast(xs, ys).shape
        out = np.zeros(shape)
        if self.previous is not None:
            out += self.previous(comp, neighbors, xs, ys)
        present = {c.ref: c for c in neighbors}
        present[comp.ref] = comp
        rows = [
            row
            for row in self.cost.rows
            if comp.ref in row["refs"] and set(row["refs"]) <= present.keys()
        ]
        if not rows:
            return out
        points = []
        for c in self.graph.components:
            for _, xy in pin_positions(present.get(c.ref, c)):
                points.append(xy)
        xy = np.broadcast_to(np.asarray(points), shape + (len(points), 2)).copy()
        own = np.array([r == comp.ref for r in self.cost.owners])
        xy[..., own, 0] += np.asarray(xs)[..., None] - comp.pos[0]
        xy[..., own, 1] += np.asarray(ys)[..., None] - comp.pos[1]
        wanted = {row["id"] for row in rows}
        for row, value in self.cost.terms(xy):
            if row["id"] in wanted:
                out += WEIGHT * value
        return out
