"""The routing effects of a board's custom rules on the detailed grid
(``board.dru_routing: true``; the effects come from :mod:`pnr.dru_rules`).

route_board calls these in order:

* :func:`raised_clearances` before the halos are sized: a pair clearance up to
  ``PAIR_RAISE_MAX_MM`` raises the clearance of the side with fewer nets, so every
  net of that side keeps it from everything (the other side's nets included);
* :func:`layer_masks` once the grid exists: the layers a net's tracks may use
  (``disallow track`` on layers), and the one layer of a net that may not change
  layer (``disallow via``: its pads' layer);
* :func:`pair_keepouts` once the escapes are planned: for a larger pair clearance
  each side's tracks and vias are barred (net keep-outs) within the clearance of
  the other side's pads, escape copper and fixed copper, both ways (cells are judged
  at their centres, so the reach carries :func:`step_margin`, what a 45-degree step
  between two free centres can come closer);
* :func:`pair_route_offenders` once the maze has routed: two routed nets of a larger
  pair rule closer than its clearance (exact distances) are a conflict; the fewest
  nets that leave none are barred from the other side's routed copper (net keep-outs)
  and handed to the class repair (:func:`.class_check.repair`, which ``dru_routing``
  implies), which routes them again around it;
* :func:`length_report` on the finished copper: each length-limited net's routed
  length against its limit.

The copper audit (:func:`.class_check.copper_audit`, ``pairs``) judges the pairs on
the emitted copper. Every effect is reported (``escape_diagnostics.dru``).
"""

from __future__ import annotations

import math
from typing import Dict, Optional


def raised_clearances(dru: Optional[Dict]) -> Dict[str, float]:
    """net -> clearance (mm) a small pair clearance raises (module doc)."""
    from pnr.dru_rules import PAIR_RAISE_MAX_MM

    out: Dict[str, float] = {}
    for pair in (dru or {}).get("pair_clearances", []):
        d = float(pair["clearance_mm"])
        if d > PAIR_RAISE_MAX_MM + 1e-9:
            continue
        side = pair["a"] if len(pair["a"]) <= len(pair["b"]) else pair["b"]
        for n in side:
            out[n] = max(out.get(n, 0.0), d)
    return out


def pair_needs(dru: Optional[Dict]) -> Dict[frozenset, float]:
    """``{frozenset({a, b}): clearance}`` of every pair rule (for the audit)."""
    out: Dict[frozenset, float] = {}
    for pair in (dru or {}).get("pair_clearances", []):
        d = float(pair["clearance_mm"])
        for a in pair["a"]:
            for b in pair["b"]:
                if a != b:
                    key = frozenset((a, b))
                    out[key] = max(out.get(key, 0.0), d)
    return out


def layer_masks(grid, graph, dru: Optional[Dict]):
    """``(masks, report)``: net -> the grid layer indices its tracks may use (only
    nets the rules restrict), and what was applied or could not be."""
    from pnr.place.geometry import pad_rects

    from .grid import pad_layer

    names = list(grid.layers)
    everything = frozenset(range(len(names)))
    masks: Dict[str, frozenset] = {}
    report = {"track_layers": 0, "no_via": [], "no_via_left": {}}
    for net, allowed in sorted(((dru or {}).get("track_layers") or {}).items()):
        idx = frozenset(names.index(la) for la in allowed if la in names)
        if idx != everything:
            masks[net] = idx
            report["track_layers"] += 1
    pads: Dict[str, set] = {}
    for comp in graph.components:
        for (_name, net, _r), pad in zip(pad_rects(comp), comp.pads):
            if net:
                # An SMD land's own layer (a far-side land's is the opposite outer one).
                own = {pad_layer(grid, comp, pad)}
                pads.setdefault(net, set()).update(everything if pad.through_hole else own)
    for net in (dru or {}).get("no_via", []):
        layers = pads.get(net)
        if not layers:
            continue
        if len(layers) != 1:
            report["no_via_left"][net] = "pads on %d layers (a through-hole pad joins them)" % len(
                layers
            )
            continue
        keep = masks.get(net, everything) & frozenset(layers)
        if not keep:
            report["no_via_left"][net] = "its pads' layer is barred to its tracks"
            continue
        masks[net] = keep
        report["no_via"].append(net)
    return masks, report


def _copper_of(grid, nets, fixed_copper):
    """Static copper of ``nets``: ``(layer index or None for every layer, kind,
    geometry, half width)``; kinds rect (a pad), seg (escape or fixed track), via."""
    out = []
    for la, owner, r in grid.pad_rectangles:
        if owner in nets:
            out.append((la, "rect", r, 0.0))
    for la, owner, a, b in grid.escape_segments:
        if owner in nets:
            out.append((la, "seg", (a, b), grid.net_widths.get(owner, grid.track_width) / 2))
    for owner, p in grid.escape_vias:
        if owner in nets:
            out.append((None, "seg", (p, p), grid.via_radius))
    # Fixed copper (pnr.fixed_copper schema 2 blocks): tracks [net, layer, a, b, w],
    # arcs [net, layer, start, mid, end, w] (as chords of under 3 degrees),
    # vias {net, xy, diameter_mm} and polygons {net, layer, outline} (by their
    # boundary).
    from pnr.board_edge import _arc_points

    index = {name: k for k, name in enumerate(grid.layers)}
    blocks = (fixed_copper or {}).get("blocks", []) if isinstance(fixed_copper, dict) else []
    for block in blocks:
        for t in block.get("tracks", []):
            if t[0] in nets and t[1] in index:
                width = t[4] if len(t) > 4 else grid.track_width
                out.append((index[t[1]], "seg", (tuple(t[2]), tuple(t[3])), width / 2))
        for arc in block.get("arcs") or []:
            if arc[0] in nets and arc[1] in index:
                width = arc[5] if len(arc) > 5 else grid.track_width
                pts = _arc_points({"a": arc[2], "m": arc[3], "b": arc[4]})
                for a, b in zip(pts, pts[1:]):
                    out.append((index[arc[1]], "seg", (a, b), width / 2))
        for v in block.get("vias", []):
            if v.get("net") in nets and len(v.get("xy") or ()) == 2:
                p = tuple(v["xy"])
                out.append((None, "seg", (p, p), float(v.get("diameter_mm", 0.4)) / 2))
        for poly in block.get("polygons") or []:
            if poly.get("net") in nets and poly.get("layer") in index:
                ring = [tuple(q) for q in poly.get("outline") or ()]
                for a, b in zip(ring, ring[1:] + ring[:1]):
                    out.append((index[poly["layer"]], "seg", (a, b), 0.0))
    return out


def _mask_near(grid, items, reach_of):
    """``[nlayers, ny, nx]`` bool: cell centres within ``reach_of(half)`` plus the
    item's own extent of an item on that layer (every layer for None)."""
    import numpy as np

    out = np.zeros((grid.nlayers, grid.ny, grid.nx), dtype=bool)
    p = grid.pitch
    for la, kind, geom, half in items:
        reach = reach_of(half)
        if kind == "rect":
            x0, y0, x1, y1 = geom.left, geom.bottom, geom.right, geom.top
        else:
            (a, b) = geom
            x0, y0 = min(a[0], b[0]), min(a[1], b[1])
            x1, y1 = max(a[0], b[0]), max(a[1], b[1])
        i0 = max(0, int(math.floor((x0 - reach) / p)) - 1)
        i1 = min(grid.nx - 1, int(math.floor((x1 + reach) / p)) + 1)
        j0 = max(0, int(math.floor((y0 - reach) / p)) - 1)
        j1 = min(grid.ny - 1, int(math.floor((y1 + reach) / p)) + 1)
        if i1 < i0 or j1 < j0:
            continue
        xs = (np.arange(i0, i1 + 1) + 0.5) * p
        ys = (np.arange(j0, j1 + 1) + 0.5) * p
        X, Y = np.meshgrid(xs, ys)
        if kind == "rect":
            dx = np.maximum(np.maximum(x0 - X, 0.0), X - x1)
            dy = np.maximum(np.maximum(y0 - Y, 0.0), Y - y1)
            d = np.hypot(dx, dy)
        else:
            ax, ay = a
            vx, vy = b[0] - ax, b[1] - ay
            den = vx * vx + vy * vy
            t = 0.0 if den == 0 else np.clip(((X - ax) * vx + (Y - ay) * vy) / den, 0.0, 1.0)
            d = np.hypot(X - ax - t * vx, Y - ay - t * vy)
        near = d < reach - 1e-9
        layers = range(grid.nlayers) if la is None else (la,)
        for layer in layers:
            out[layer, j0 : j1 + 1, i0 : i1 + 1] |= near
    return out


def step_margin(grid) -> float:
    """How much closer than its two cell centres a 45-degree step between them can
    pass a point (``pitch (1 - 1/sqrt 2)``): a keep-out judged at cell centres grows
    by it."""
    return grid.pitch * (1.0 - 1.0 / math.sqrt(2.0))


def _route_items(grid, rn, width):
    """A routed net's copper as :func:`_mask_near` items: its segments (half the track
    width) and its vias (every layer, the via radius)."""
    out = []
    for la, p, q in rn.segments:
        out.append((la, "seg", (grid.center_of(*p), grid.center_of(*q)), width / 2))
    for i, j in rn.vias:
        c = grid.center_of(i, j)
        out.append((None, "seg", (c, c), grid.via_radius))
    return out


def _items_gap(a_items, b_items):
    """The least edge-to-edge distance (mm) between two item lists (same layer, or a
    via against anything)."""
    from pnr.writeback import _segment_distance_sq

    best = math.inf
    for la, _k, (p, q), ha in a_items:
        for lb, _k2, (r, t), hb in b_items:
            if la is not None and lb is not None and la != lb:
                continue
            d = math.sqrt(_segment_distance_sq(p, q, r, t)) - ha - hb
            if d < best:
                best = d
    return best


def pair_route_offenders(grid, nets, result, widths, dru: Optional[Dict]):
    """The routed nets of ``result`` that break a pair clearance above
    ``PAIR_RAISE_MAX_MM`` against another routed net (module doc). For the fewest nets
    that leave no such pair (the net in most pairs first, then fewer routed cells,
    then the name), net keep-outs bar their tracks and vias from the other side's
    routed copper (reach: the clearance, the copper's half width, the net's half
    width or via radius, and :func:`step_margin`). Returns ``({net: rows}, report)``:
    rows carry the net's routed ``cells`` inside those keep-outs (the class repair
    prunes them when it keeps what fits) and the pair."""
    from pnr.dru_rules import PAIR_RAISE_MAX_MM

    from .class_check import _cover
    from .grid import Cell

    rules = [
        p for p in (dru or {}).get("pair_clearances", []) if float(p["clearance_mm"]) > PAIR_RAISE_MAX_MM + 1e-9
    ]
    report = {"pairs": [], "ripped": []}
    if not rules:
        return {}, report
    routed = {n for n, rn in result.nets.items() if rn.cells}
    items = {}

    def copper(net):
        if net not in items:
            rn = result.nets[net]
            items[net] = _route_items(grid, rn, widths.get(net, grid.track_width))
        return items[net]

    conflicts = {}  # (a, b) sorted -> (clearance, gap)
    for pair in rules:
        d = float(pair["clearance_mm"])
        for a in pair["a"]:
            for b in pair["b"]:
                if a == b or a not in routed or b not in routed:
                    continue
                gap = _items_gap(copper(a), copper(b))
                if gap < d - 1e-6:
                    key = tuple(sorted((a, b)))
                    old = conflicts.get(key)
                    if old is None or d > old[0]:
                        conflicts[key] = (d, gap)
    if not conflicts:
        return {}, report
    sizes = {n: len(result.nets[n].cells) for n in routed}
    ripped = _cover(set(conflicts), sizes)
    every = frozenset(nets)
    margin = step_margin(grid)
    out = {}
    for net in ripped:
        others = []
        for (a, b), (d, gap) in sorted(conflicts.items()):
            if net not in (a, b):
                continue
            other = b if a == net else a
            if other in ripped:
                continue
            others.append((other, d, gap))
        if not others:
            continue
        w = widths.get(net, grid.track_width)
        track = via = None
        for other, d, _gap in others:
            t = _mask_near(grid, copper(other), lambda half, d=d: d + half + w / 2 + margin)
            v = _mask_near(grid, copper(other), lambda half, d=d: d + half + grid.via_radius + margin)
            track = t if track is None else track | t
            via = v if via is None else via | v
        via = via.any(axis=0)[None, :, :].repeat(grid.nlayers, axis=0)
        grid.add_net_keepout(track, via, every - {net})
        rn = result.nets[net]
        vias = set(rn.vias)
        cells = [
            [c.layer, c.i, c.j]
            for c in rn.cells
            if track[c.layer, c.j, c.i] or ((c.i, c.j) in vias and via[c.layer, c.j, c.i])
        ]
        out[net] = [
            dict(pair=[net, other], need_mm=d, gap_mm=round(gap, 4), cells=cells)
            for other, d, gap in others
        ]
    report["pairs"] = [
        dict(nets=list(k), need_mm=v[0], gap_mm=round(v[1], 4)) for k, v in sorted(conflicts.items())
    ][:20]
    report["pair_count"] = len(conflicts)
    report["ripped"] = sorted(out)
    return out, report


def pair_keepouts(grid, nets, dru: Optional[Dict], fixed_copper=None) -> list:
    """Net keep-outs for the pair clearances above ``PAIR_RAISE_MAX_MM`` (module
    doc): one per side and pair rule, barring that side's tracks within ``d +
    ½width`` and its vias within ``d + via radius`` of the other side's copper
    (``d + extent``), plus :func:`step_margin`. Returns the report rows."""
    from pnr.dru_rules import PAIR_RAISE_MAX_MM

    every = frozenset(nets)
    margin = step_margin(grid)
    rows = []
    for pair in (dru or {}).get("pair_clearances", []):
        d = float(pair["clearance_mm"])
        if d <= PAIR_RAISE_MAX_MM + 1e-9:
            continue
        for side, other in ((pair["a"], pair["b"]), (pair["b"], pair["a"])):
            side = [n for n in side if n in every]
            copper = _copper_of(grid, set(other), fixed_copper)
            if not side or not copper:
                rows.append({"rule": pair["rule"], "nets": side, "copper": len(copper), "cells": 0})
                continue
            width = max(grid.net_widths.get(n, grid.track_width) for n in side)
            track = _mask_near(grid, copper, lambda half: d + half + width / 2 + margin)
            via = _mask_near(grid, copper, lambda half: d + half + grid.via_radius + margin)
            via = via.any(axis=0)[None, :, :].repeat(grid.nlayers, axis=0)
            grid.add_net_keepout(track, via, every - frozenset(side))
            rows.append(
                {
                    "rule": pair["rule"],
                    "clearance_mm": d,
                    "nets": side,
                    "copper": len(copper),
                    "cells": int(track.sum()),
                }
            )
    return rows


def length_report(tracks, dru: Optional[Dict]) -> Dict:
    """``{net: {length_mm, max_mm, ok}}`` for the length-limited nets, from the
    emitted ``tracks`` ``(net, layer, a, b, width)`` (escapes and routes; vias add
    nothing here)."""
    limits = (dru or {}).get("length_max") or {}
    lengths: Dict[str, float] = {}
    for net, _layer, a, b, _w in tracks:
        if net in limits:
            lengths[net] = lengths.get(net, 0.0) + math.dist(a, b)
    return {
        net: {
            "length_mm": round(lengths.get(net, 0.0), 3),
            "max_mm": limit,
            "ok": lengths.get(net, 0.0) <= limit + 1e-9,
        }
        for net, limit in sorted(limits.items())
    }
