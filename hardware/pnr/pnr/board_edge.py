"""The board's own Edge.Cuts outline, as KiCad's DRC judges it (``board.edge: exact``).

By default the router models the board edge as the placement rectangle and keeps
copper and holes an inset from it, and writeback stamps that rectangle as the new
outline (a 0.15 mm stroke). A board whose outline has rounded corners, notches or
another stroke then loses its shape, and a hole-to-edge rule written for its own
stroke (KiCad measures a hole to the stroke's edge) no longer matches the board.

With ``board.edge: exact`` (rules key ``edge``) the drivers attach the source
outline to the rules (:func:`attach_edges`: line, arc and circle items in the
engine frame plus the stroke, ``rules["board_edges"]``); the router keeps every
track ``edge_clearance + ½width`` and every via the larger of ``edge_clearance +
via radius`` and the hole-to-edge rule from the outline's centre line
(:func:`grid_edge_masks`), judged on that contour, so rounded corners count; and
writeback keeps the source outline instead of the rectangle (implied
``keep_outline``; :func:`kept_outline_text`).

The hole-to-edge distance a via centre needs from the centre line is the larger of
the fab's physical rule (``hole_to_edge_mm + ½drill``) and what KiCad's custom rule
asks (:func:`via_edge_inset`): ``limit + ½stroke + ½drill`` with ``limit`` the
board's own ``physical_hole_clearance`` to ``Edge.Cuts`` when its custom rules give
one (``rules["dru"]``, pnr.dru_rules), else the fab profile's, which assumes the
board's stroke. A 1 µm margin keeps a via off the threshold.

Stdlib only (the KiCad-side writeback imports it); the masks use numpy lazily.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

# Page offset (mm) of writeback's frame: pnr.writeback._PAGE_OFFSET_MM.
PAGE_OFFSET_MM = 30.0
MARGIN_MM = 0.001

_ITEM = re.compile(r"\(\s*gr_(line|arc|circle|rect|poly)\b")
_PAIR = r"\(\s*%s\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)"


def _blocks(text: str):
    """``(kind, block text, start, end)`` of every board-level ``gr_*`` item."""
    pos = 0
    while True:
        m = _ITEM.search(text, pos)
        if not m:
            return
        depth, j = 0, m.start()
        while j < len(text):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        yield m.group(1), text[m.start() : j], m.start(), j
        pos = j


def _point(block: str, key: str) -> Optional[Tuple[float, float]]:
    m = re.search(_PAIR % key, block)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _edge_items(text: str):
    """Edge.Cuts items of a ``.kicad_pcb`` text in pcbnew mm (y down): dicts with
    ``kind`` line/arc/circle and their points, plus their stroke widths."""
    items, strokes = [], []
    for kind, block, _, _ in _blocks(text):
        if '(layer "Edge.Cuts")' not in block and "(layer Edge.Cuts)" not in block:
            continue
        w = re.search(r"\(\s*stroke\s*\(\s*width\s+([\d.eE+-]+)", block) or re.search(
            r"\(\s*width\s+([\d.eE+-]+)", block
        )
        strokes.append(float(w.group(1)) if w else 0.0)
        if kind == "line":
            items.append({"kind": "line", "a": _point(block, "start"), "b": _point(block, "end")})
        elif kind == "arc":
            items.append(
                {
                    "kind": "arc",
                    "a": _point(block, "start"),
                    "m": _point(block, "mid"),
                    "b": _point(block, "end"),
                }
            )
        elif kind == "circle":
            c, e = _point(block, "center"), _point(block, "end")
            items.append({"kind": "circle", "c": c, "r": math.dist(c, e)})
        elif kind == "rect":
            (x0, y0), (x1, y1) = _point(block, "start"), _point(block, "end")
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            items += [
                {"kind": "line", "a": corners[k], "b": corners[(k + 1) % 4]} for k in range(4)
            ]
        else:  # poly
            pts = [
                (float(a), float(b))
                for a, b in re.findall(r"\(\s*xy\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)", block)
            ]
            items += [
                {"kind": "line", "a": pts[k], "b": pts[(k + 1) % len(pts)]}
                for k in range(len(pts))
                if len(pts) > 1
            ]
    return items, strokes


def _arc_geometry(a, m, b):
    """Centre, radius, start angle and signed sweep (radians) of the arc a-m-b."""
    ax, ay = a
    mx, my = m
    bx, by = b
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-12:
        return None
    ux = (
        (ax**2 + ay**2) * (my - by) + (mx**2 + my**2) * (by - ay) + (bx**2 + by**2) * (ay - my)
    ) / d
    uy = (
        (ax**2 + ay**2) * (bx - mx) + (mx**2 + my**2) * (ax - bx) + (bx**2 + by**2) * (mx - ax)
    ) / d
    r = math.dist((ux, uy), a)
    t0 = math.atan2(ay - uy, ax - ux)
    tm = math.atan2(my - uy, mx - ux)
    t1 = math.atan2(by - uy, bx - ux)

    def ccw(x, y):  # angle from x to y going counter-clockwise, in [0, 2pi)
        return (y - x) % (2 * math.pi)

    sweep = ccw(t0, t1)
    if ccw(t0, tm) > sweep:  # the mid point is on the other way round
        sweep -= 2 * math.pi
    return (ux, uy), r, t0, sweep


def _arc_points(item, steps=None):
    geo = _arc_geometry(item["a"], item["m"], item["b"])
    if geo is None:
        return [item["a"], item["b"]]
    (cx, cy), r, t0, sweep = geo
    n = steps or max(4, int(math.ceil(abs(sweep) / (math.pi / 64))))
    return [
        (cx + r * math.cos(t0 + sweep * k / n), cy + r * math.sin(t0 + sweep * k / n))
        for k in range(n + 1)
    ]


def _extent(items) -> Tuple[float, float, float, float]:
    xs, ys = [], []
    for it in items:
        if it["kind"] == "line":
            pts = [it["a"], it["b"]]
        elif it["kind"] == "arc":
            pts = _arc_points(it, 720)
        else:
            (cx, cy), r = it["c"], it["r"]
            pts = [(cx - r, cy - r), (cx + r, cy + r)]
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def parse_edges(text: str) -> Optional[Dict]:
    """The board outline of a ``.kicad_pcb`` text in the engine frame (mm, y up,
    origin at the outline's bottom-left, as pnr.ingest frames the board):
    ``{"stroke_mm", "size": [w, h], "origin": [left, top] (pcbnew mm), "items"}``;
    None when the board has no Edge.Cuts item."""
    items, strokes = _edge_items(text)
    if not items:
        return None
    left, top, right, bottom = _extent(items)

    def eng(p):
        return [round(p[0] - left, 9), round(bottom - p[1], 9)]

    out = []
    for it in items:
        if it["kind"] == "line":
            out.append({"kind": "line", "a": eng(it["a"]), "b": eng(it["b"])})
        elif it["kind"] == "arc":
            out.append({"kind": "arc", "a": eng(it["a"]), "m": eng(it["m"]), "b": eng(it["b"])})
        else:
            out.append({"kind": "circle", "c": eng(it["c"]), "r": it["r"]})
    return {
        "stroke_mm": max(strokes) if strokes else 0.0,
        "size": [round(right - left, 9), round(bottom - top, 9)],
        "origin": [round(left, 9), round(top, 9)],
        "items": out,
    }


def attach_edges(rules: Dict, board_text: str) -> None:
    """``rules["board_edges"]`` from the board's outline when the rules ask for the
    exact edge (``edge: exact``); nothing otherwise."""
    if (rules or {}).get("edge") != "exact":
        return
    edges = parse_edges(board_text)
    if edges is not None:
        rules["board_edges"] = edges


def keep_outline(rules: Optional[Dict]) -> bool:
    """Writeback keeps the source outline (``keep_outline: true``, or ``edge: exact``)."""
    rules = rules or {}
    return bool(rules.get("keep_outline")) or rules.get("edge") == "exact"


# ----------------------------------------------------------------- the contour


def _loops(items, tol=1e-4) -> List[List[Tuple[float, float]]]:
    """The outline as closed point loops (arcs flattened, circles as 256-gons)."""
    chains = []
    for it in items:
        if it["kind"] == "circle":
            (cx, cy), r = it["c"], it["r"]
            chains.append(
                [
                    (
                        cx + r * math.cos(2 * math.pi * k / 256),
                        cy + r * math.sin(2 * math.pi * k / 256),
                    )
                    for k in range(257)
                ]
            )
            continue
        pts = [tuple(it["a"]), tuple(it["b"])] if it["kind"] == "line" else _arc_points(it)
        chains.append(list(pts))
    loops, open_chains = [], []
    for c in chains:
        if math.dist(c[0], c[-1]) < tol and len(c) > 2:
            loops.append(c)
        else:
            open_chains.append(c)
    while open_chains:
        loop = open_chains.pop(0)
        grown = True
        while grown and math.dist(loop[0], loop[-1]) >= tol:
            grown = False
            for k, c in enumerate(open_chains):
                if math.dist(loop[-1], c[0]) < tol:
                    loop += c[1:]
                elif math.dist(loop[-1], c[-1]) < tol:
                    loop += list(reversed(c))[1:]
                else:
                    continue
                open_chains.pop(k)
                grown = True
                break
        loops.append(loop)
    return loops


def via_edge_inset(rules: Dict, edges: Dict, via_radius: float, drill: float) -> float:
    """The least centre-line distance (mm) a via centre needs from the outline
    (module doc), the 1 µm margin included."""
    fab = dict((rules or {}).get("fab") or {})
    stroke = float(edges.get("stroke_mm") or 0.0)
    need = float(fab.get("edge_clearance_mm", 0.2)) + via_radius
    hole = fab.get("hole_to_edge_mm")
    if hole is not None:
        # The physical rule; the fab profile's custom rule (fab_profile.dru_text)
        # writes it less half this board's stroke, the same distance.
        need = max(need, float(hole) + drill / 2)
    limit = ((rules or {}).get("dru") or {}).get("hole_to_edge_mm")
    if limit is not None:  # the board's own custom rule (pnr.dru_rules)
        need = max(need, float(limit) + stroke / 2 + drill / 2)
    return need + MARGIN_MM


def track_edge_inset(rules: Dict, track_width: float) -> float:
    """The least centre-line distance a track's centre line needs from the outline."""
    fab = dict((rules or {}).get("fab") or {})
    return float(fab.get("edge_clearance_mm", 0.2)) + track_width / 2


def grid_edge_masks(grid, edges: Dict):
    """``(outside, distance)`` per grid cell centre (``[ny, nx]``): outside the
    board's outline (or inside one of its cut-outs), and the distance (mm) to the
    nearest outline item's centre line."""
    import numpy as np

    xs = (np.arange(grid.nx) + 0.5) * grid.pitch
    ys = (np.arange(grid.ny) + 0.5) * grid.pitch
    X, Y = np.meshgrid(xs, ys)
    dist = np.full(X.shape, np.inf)
    for it in edges["items"]:
        if it["kind"] == "line":
            segs = [(it["a"], it["b"])]
        elif it["kind"] == "circle":
            (cx, cy), r = it["c"], it["r"]
            dist = np.minimum(dist, np.abs(np.hypot(X - cx, Y - cy) - r))
            continue
        else:
            geo = _arc_geometry(it["a"], it["m"], it["b"])
            if geo is None:
                segs = [(it["a"], it["b"])]
            else:
                (cx, cy), r, t0, sweep = geo
                ang = np.arctan2(Y - cy, X - cx)
                rel = np.mod(ang - t0, 2 * np.pi) if sweep >= 0 else np.mod(t0 - ang, 2 * np.pi)
                on = rel <= abs(sweep) + 1e-12
                d_arc = np.abs(np.hypot(X - cx, Y - cy) - r)
                d_end = np.minimum(
                    np.hypot(X - it["a"][0], Y - it["a"][1]),
                    np.hypot(X - it["b"][0], Y - it["b"][1]),
                )
                dist = np.minimum(dist, np.where(on, d_arc, d_end))
                continue
        for a, b in segs:
            ax, ay = a
            dx, dy = b[0] - ax, b[1] - ay
            den = dx * dx + dy * dy
            t = 0.0 if den == 0 else np.clip(((X - ax) * dx + (Y - ay) * dy) / den, 0.0, 1.0)
            dist = np.minimum(dist, np.hypot(X - ax - t * dx, Y - ay - t * dy))
    inside = np.zeros(X.shape, dtype=int)
    for loop in _loops(edges["items"]):
        hit = np.zeros(X.shape, dtype=bool)
        for k in range(len(loop) - 1):
            x0, y0 = loop[k]
            x1, y1 = loop[k + 1]
            if y0 == y1:
                continue
            crosses = (y0 > Y) != (y1 > Y)
            xc = x0 + (Y - y0) / (y1 - y0) * (x1 - x0)
            hit ^= crosses & (X < xc)
        inside += hit
    # Even-odd: inside the outer loop and no cut-out.
    return (inside % 2) == 0, dist


def block_exact_edge(grid, rules: Dict, edges: Dict) -> Dict:
    """Bar tracks and vias of every cell the exact outline forbids (module doc):
    outside it, a track centre within :func:`track_edge_inset`, a via within
    :func:`via_edge_inset`. Pad cells keep their tracks (an edge connector's access,
    as the rectangle model does), never a via. Returns a report."""
    import numpy as np

    fab = dict((rules or {}).get("fab") or {})
    drill = float(fab.get("via_drill_mm", 2 * grid.via_drill_radius))
    outside, dist = grid_edge_masks(grid, edges)
    track_in = track_edge_inset(rules, grid.track_width)
    via_in = via_edge_inset(rules, edges, grid.via_radius, drill)
    track = outside | (dist < track_in - 1e-9)
    via = outside | (dist < via_in - 1e-9)
    pads = np.zeros((grid.nlayers, grid.ny, grid.nx), dtype=bool)
    for la, i, j in grid.pad_net:
        if 0 <= la < grid.nlayers and 0 <= i < grid.nx and 0 <= j < grid.ny:
            pads[la, j, i] = True
    before = int(grid.via_blocked.sum())
    grid.blocked |= track[None, :, :] & ~pads
    grid.via_blocked |= via[None, :, :]
    return {
        "stroke_mm": edges.get("stroke_mm"),
        "track_inset_mm": round(track_in, 6),
        "via_inset_mm": round(via_in, 6),
        "items": len(edges["items"]),
        "via_cells_added": int(grid.via_blocked.sum()) - before,
    }


# ------------------------------------------------------------------ writeback


def outline_text(
    width: float,
    height: float,
    radius: float = 0.0,
    stroke: float = 0.15,
    offset: float = PAGE_OFFSET_MM,
    uuid_prefix: str = "b0ad0012-0000-4000-8000",
) -> str:
    """Edge.Cuts lines (and corner arcs for ``radius`` > 0) of a ``width`` x
    ``height`` rectangle at the page ``offset``, as ``.kicad_pcb`` text."""
    x0, y0, x1, y1 = offset, offset, offset + width, offset + height
    r = max(0.0, min(radius, width / 2, height / 2))
    lines = [
        ((x0 + r, y0), (x1 - r, y0)),
        ((x1, y0 + r), (x1, y1 - r)),
        ((x1 - r, y1), (x0 + r, y1)),
        ((x0, y1 - r), (x0, y0 + r)),
    ]
    k = 0.7071067811865476
    arcs = [
        ((x1 - r, y0), (x1 - r + r * k, y0 + r - r * k), (x1, y0 + r)),
        ((x1, y1 - r), (x1 - r + r * k, y1 - r + r * k), (x1 - r, y1)),
        ((x0 + r, y1), (x0 + r - r * k, y1 - r + r * k), (x0, y1 - r)),
        ((x0, y0 + r), (x0 + r - r * k, y0 + r - r * k), (x0 + r, y0)),
    ]
    out, n = [], 0
    for a, b in lines:
        out.append(
            "  (gr_line (start %.6f %.6f) (end %.6f %.6f)\n"
            '    (stroke (width %g) (type solid)) (layer "Edge.Cuts")\n'
            '    (uuid "%s-%012d"))' % (a[0], a[1], b[0], b[1], stroke, uuid_prefix, n)
        )
        n += 1
    if r > 0:
        for a, m, b in arcs:
            out.append(
                "  (gr_arc (start %.6f %.6f) (mid %.6f %.6f) (end %.6f %.6f)\n"
                '    (stroke (width %g) (type solid)) (layer "Edge.Cuts")\n'
                '    (uuid "%s-%012d"))'
                % (a[0], a[1], m[0], m[1], b[0], b[1], stroke, uuid_prefix, n)
            )
            n += 1
    return "\n".join(out) + "\n"


def kept_outline_text(text: str, width: float, height: float) -> Optional[str]:
    """``text`` with its Edge.Cuts items moved to writeback's frame (the outline's
    top-left at the page offset), or None when they cannot frame the placement
    region (no outline, or an outline whose size is not ``width`` x ``height``
    within 1 µm: the caller then stamps the rectangle)."""
    edges = parse_edges(text)
    if edges is None:
        return None
    w, h = edges["size"]
    if abs(w - width) > 1e-3 or abs(h - height) > 1e-3:
        return None
    dx = PAGE_OFFSET_MM - edges["origin"][0]
    dy = PAGE_OFFSET_MM - edges["origin"][1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return text
    out, pos = [], 0
    for _kind, block, start, end in _blocks(text):
        if '(layer "Edge.Cuts")' not in block and "(layer Edge.Cuts)" not in block:
            continue

        def move(m):
            return "(%s %s %s)" % (
                m.group(1),
                _fmt(float(m.group(2)) + dx),
                _fmt(float(m.group(3)) + dy),
            )

        moved = re.sub(
            r"\(\s*(start|end|mid|center|xy)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s*\)", move, block
        )
        out.append(text[pos:start])
        out.append(moved)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _fmt(v: float) -> str:
    return ("%.6f" % v).rstrip("0").rstrip(".")
