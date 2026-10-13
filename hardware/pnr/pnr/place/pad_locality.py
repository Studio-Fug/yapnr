"""Pad-specific attachment for small parts around a multi-net component.

PNR_PAD_LOCALITY=1 adds a proximity objective; it does not invent electrical
limits. Authored pad-group ownership wins. Otherwise a private two-component net
and a second shared net establish an unambiguous local attachment. Shared
power/ground alone never assign a capacitor to an arbitrary IC.
"""

import math
import os

WEIGHT = 4.0
EPS2 = 0.01


def enabled():
    return os.environ.get("PNR_PAD_LOCALITY", "1") == "1"


def attachments(graph, constraints):
    """Pure, flag-independent obligations, including every pad of each branch.

    Indices refer to the graph's full physical pad order, so duplicate pad numbers
    remain alternatives rather than being overwritten by a dictionary lookup.
    """
    by_ref = {c.ref: c for c in graph.components}
    pads = {}
    number = 0
    for comp in graph.components:
        for pad in comp.pads:
            if pad.net:
                pads.setdefault(comp.ref, {}).setdefault(pad.net, []).append((number, pad.name))
            number += 1
    nets = {}
    for ref, rows in pads.items():
        for net in rows:
            nets.setdefault(net, set()).add(ref)
    declared = {}
    for con in constraints.constraints:
        if (
            con.kind == "group"
            and con.params.get("anchor") in by_ref
            and con.params.get("anchor_pad") is not None
        ):
            anchor = con.params["anchor"]
            for ref in con.refs:
                if ref != anchor:
                    declared.setdefault(ref, set()).add(anchor)
    result = []
    for ref, rows in sorted(pads.items()):
        if len(rows) != 2 or len({p.name for p in by_ref[ref].pads if p.net}) != 2:
            continue
        candidates = [
            a
            for a, other in pads.items()
            if a != ref and len(other) >= 3 and set(rows) <= set(other)
        ]
        authored = [a for a in candidates if a in declared.get(ref, ())]
        private = [a for a in candidates if any(nets[n] == {ref, a} for n in rows)]
        owners = authored or private
        if len(owners) != 1:
            continue
        anchor = owners[0]
        links = []
        for net, entries in sorted(rows.items()):
            # One obligation per logical member terminal; duplicate physical
            # shapes are alternatives, not separate electrical branches.
            for name in sorted({name for _, name in entries}):
                links.append(
                    dict(
                        net=net,
                        pad=name,
                        source=[i for i, label in entries if label == name],
                        targets=[i for i, _ in pads[anchor][net]],
                        target_pads=sorted({name for _, name in pads[anchor][net]}),
                    )
                )
        result.append(
            dict(
                member=ref,
                anchor=anchor,
                links=links,
                provenance="authored-group" if authored else "private-net",
            )
        )
    return result


class Cost:
    """Nearest matching physical pad per terminal, not net bbox or hub centroid."""

    def __init__(self, graph, constraints):
        self.attachments = attachments(graph, constraints)
        self.links = [link for relation in self.attachments for link in relation["links"]]

    def torch(self, xy):
        import torch

        total = xy.new_zeros(xy.shape[:-2])
        for link in self.links:
            a = xy[..., link["source"], :]
            b = xy[..., link["targets"], :]
            distance = torch.sqrt(((a[..., :, None, :] - b[..., None, :, :]) ** 2).sum(-1) + EPS2)
            total = total + distance.flatten(-2).min(-1).values
        return total

    def numpy_terms(self, xy):
        import numpy as np

        for relation in self.attachments:
            value = np.zeros(xy.shape[:-2])
            for link in relation["links"]:
                a, b = xy[..., link["source"], :], xy[..., link["targets"], :]
                d = np.sqrt(((a[..., :, None, :] - b[..., None, :, :]) ** 2).sum(-1) + EPS2)
                value += d.reshape(d.shape[:-2] + (-1,)).min(-1)
            yield relation, value


def audit(graph, constraints, limits=None):
    """Independent exact pad distances, with optional explicit per-link limits.

    Missing limits are reported as unqualified, never as electrical acceptance.
    Limits are keyed by (member, pad, anchor); inference itself is not a limit.
    """
    from .geometry import pin_positions

    points = [point for comp in graph.components for _, point in pin_positions(comp)]
    rows = []
    for relation in attachments(graph, constraints):
        for link in relation["links"]:
            distance, a, b = min(
                (math.dist(points[i], points[j]), i, j)
                for i in link["source"]
                for j in link["targets"]
            )
            key = (relation["member"], link["pad"], relation["anchor"])
            limit = (limits or {}).get(key)
            if limit is not None and (not math.isfinite(limit) or limit < 0):
                raise ValueError("Pad-locality limits must be finite nonnegative distances")
            rows.append(
                dict(
                    member=relation["member"],
                    anchor=relation["anchor"],
                    pad=link["pad"],
                    net=link["net"],
                    target_pads=link["target_pads"],
                    distance_mm=distance,
                    a=points[a],
                    b=points[b],
                    limit_mm=limit,
                    passed=None if limit is None else distance <= limit,
                    provenance=relation["provenance"],
                )
            )
    return dict(
        schema="pnr-pad-locality-v1",
        links=rows,
        sum_distance_mm=sum(row["distance_mm"] for row in rows),
        qualified=bool(rows) and all(row["passed"] is True for row in rows),
        missing_limits=sum(row["limit_mm"] is None for row in rows),
        limitation="Geometric proximity only; routed loop, fanout and electrical checks remain required.",
    )


class Slots:
    """Legalization cost against already placed counterparts, with actual pad poses.

    No new hard constraint or ownership ordering is introduced. The slot search
    still honors courtyard, channel, keepout and authored placement constraints.
    """

    def __init__(self, graph, constraints):
        self.relations = attachments(graph, constraints)
        self.refs = {r[k] for r in self.relations for k in ("member", "anchor")}

    def __call__(self, comp, neighbors, xs, ys):
        import numpy as np

        from .geometry import pin_positions

        present = {c.ref: c for c in neighbors}
        total = np.zeros(np.broadcast(xs, ys).shape)
        own = [(p.net, p.name, point) for p, (_, point) in zip(comp.pads, pin_positions(comp))]
        for relation in self.relations:
            member, anchor = relation["member"], relation["anchor"]
            if comp.ref not in (member, anchor):
                continue
            other = present.get(anchor if comp.ref == member else member)
            if other is None:
                continue
            remote = [
                (p.net, p.name, point) for p, (_, point) in zip(other.pads, pin_positions(other))
            ]
            for link in relation["links"]:
                a = [
                    point
                    for net, name, point in own
                    if net == link["net"] and (comp.ref == anchor or name == link["pad"])
                ]
                b = [
                    point
                    for net, name, point in remote
                    if net == link["net"] and (comp.ref == member or name == link["pad"])
                ]
                distances = [
                    np.sqrt(
                        (xs + x - comp.pos[0] - u) ** 2 + (ys + y - comp.pos[1] - v) ** 2 + EPS2
                    )
                    for x, y in a
                    for u, v in b
                ]
                total += np.minimum.reduce(distances)
        return WEIGHT * total


def legalize_kwargs(graph, constraints):
    slot = Slots(graph, constraints) if enabled() else None
    from . import smps_cost

    if smps_cost.enabled():
        slot = smps_cost.Slots(graph, constraints, slot)
    return {"locality": slot} if slot is not None else {}


def anchor_index(graph, ref, pad):
    """Match hard pad-group geometry: the first physical shape of a named pad."""
    return next(
        (
            i
            for i, (r, p) in enumerate((c.ref, p.name) for c in graph.components for p in c.pads)
            if r == ref and p == pad
        ),
        None,
    )
