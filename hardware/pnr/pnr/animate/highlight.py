"""Constraint highlighting and live metrics, from a trace header's ``constraints`` and the poses
a frame shows (docs/design/constraint-and-hier-animations.md, section 6.2).

Only ``line_group`` and ``edge_align`` are drawn:

- a line group: a line through its members' centres in member order and, for the design that
  declares it, a dashed rectangle around the rigid body (aligned with the line);
- an edge alignment: the target edge in the constraint colour, the part's courtyard tinted, and
  a tether from the courtyard to the edge, mint within the tolerance and red beyond it.

A *reference* overlay draws another design's constraints on a board that does not declare them
(the free half of a comparison): the same shapes, neutral, never red.

Every number here is computed from recorded poses and the header; nothing is estimated.
"""

from __future__ import annotations

import math

from . import theme

EDGES = ("north", "south", "east", "west")
DRAWN = ("line_group", "edge_align")


def constraints_of(header):
    """The drawable constraints of a trace header (``[]`` when it lists none)."""
    return [c for c in header.get("constraints") or [] if c.get("kind") in DRAWN]


def extent(comp, rot):
    """``(w, h)`` of a component's courtyard under ``rot`` (quarter turns, as drawn)."""
    w, h = comp["courtyard"]
    if int(round(rot / 90.0)) % 2 == 1:
        w, h = h, w
    return w, h


def edge_distance(header, comp, pose, edge):
    """Distance (µm) from the courtyard to the named board edge (negative: past it)."""
    x, y, rot, _side = pose
    w, h = extent(comp, rot)
    width, height = header["outline"]["w"], header["outline"]["h"]
    if edge == "south":
        return y - h / 2.0
    if edge == "north":
        return height - (y + h / 2.0)
    if edge == "west":
        return x - w / 2.0
    return width - (x + w / 2.0)


def edge_tolerance(con):
    return float(con.get("tolerance_um") or 1000)


def edge_refs(constraints):
    """``[(ref, edge, tolerance)]`` of the edge alignments, in declaration order."""
    out = []
    for con in constraints:
        if con.get("kind") != "edge_align":
            continue
        for ref in con.get("refs") or []:
            out.append((ref, con.get("edge"), edge_tolerance(con)))
    return out


def on_edge(header, components, poses, constraints):
    """``(k, n)``: how many edge-aligned parts are within their tolerance of their edge."""
    targets = edge_refs(constraints)
    k = 0
    for ref, edge, tol in targets:
        if ref in poses and ref in components:
            if edge_distance(header, components[ref], poses[ref], edge) <= tol + 1.0:
                k += 1
    return k, len(targets)


def edge_order(poses, constraints):
    """``[(edge, [refs])]``: the edge-aligned parts along each edge, left to right (north and
    south) or top to bottom (east and west), from their poses."""
    edges = {}
    for ref, edge, _tol in edge_refs(constraints):
        if ref in poses:
            edges.setdefault(edge, []).append(ref)
    out = []
    for edge in sorted(edges, key=EDGES.index):
        refs = edges[edge]
        if edge in ("north", "south"):
            refs = sorted(refs, key=lambda r: (poses[r][0], r))
        else:
            refs = sorted(refs, key=lambda r: (-poses[r][1], r))
        out.append((edge, refs))
    return out


def line_error(points):
    """The largest distance (µm) of ``points`` from their least-squares (principal-axis) line."""
    if len(points) < 3:
        return 0.0
    n = float(len(points))
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    nx, ny = -math.sin(theta), math.cos(theta)
    return max(abs((p[0] - mx) * nx + (p[1] - my) * ny) for p in points)


def line_members(constraints):
    """``[(name, refs)]`` of the line groups."""
    return [
        (c.get("name") or "line", list(c.get("refs") or []))
        for c in constraints
        if c.get("kind") == "line_group"
    ]


def hpwl(pins, pin_xy):
    """Half-perimeter wirelength (µm) over nets with two or more placed pins."""
    total = 0.0
    for net in sorted(pins):
        points = [pin_xy[p] for p in pins[net] if p in pin_xy]
        if len(points) < 2:
            continue
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def legend(constraints, reference=False):
    """Short legend texts of what is highlighted (overlay text: refs, numbers, fixed words)."""
    out = []
    for name, refs in line_members(constraints):
        con = next(
            c for c in constraints if c.get("kind") == "line_group" and c.get("name") == name
        )
        span = "%s-%s" % (refs[0], refs[-1]) if len(refs) > 1 else "".join(refs)
        pitch = con.get("pitch_um")
        text = "line_group %s" % span
        if pitch:
            text += " · %.1f mm pitch" % (pitch / 1000.0)
        out.append(text)
    edges = {}
    for ref, edge, _tol in edge_refs(constraints):
        edges.setdefault(edge, []).append(ref)
    hard = any(c.get("hard") for c in constraints if c.get("kind") == "edge_align")
    for edge in sorted(edges, key=EDGES.index):
        out.append(
            "edge_align %s%s: %s" % (edge, " (hard)" if hard else "", ", ".join(edges[edge]))
        )
    if reference and out:
        return ["target, not constrained here"]
    return out


def metrics(header, components, pins, pin_xy, poses, constraints, reference=False):
    """The live metric line of a panel: HPWL and the constraint's own measure (for a reference
    overlay, without the edge order: parts off their edge have none)."""
    parts = ["HPWL %.0f mm" % (hpwl(pins, pin_xy) / 1000.0)]
    for _name, refs in line_members(constraints):
        points = [poses[r][:2] for r in refs if r in poses]
        if len(points) == len(refs):
            what = "LED line error" if all(r[:1] == "D" for r in refs) else "line error"
            parts.append("%s %.2f mm" % (what, line_error(points) / 1000.0))
    if edge_refs(constraints):
        k, n = on_edge(header, components, poses, constraints)
        parts.append("on edge %d of %d" % (k, n))
        for edge, refs in edge_order(poses, constraints) if not reference else ():
            parts.append("%s: %s" % (edge, " · ".join(refs)))
    return " · ".join(parts)


# --- drawing -----------------------------------------------------------------------------
def _mix(a, b, t):
    from .render import mix

    return mix(a, b, t)


def _rgb(color):
    from .render import rgb

    return rgb(color)


def draw_under(renderer, draw, tf, poses, constraints, reference, ss):
    """Under the copper, so pads and reference labels stay readable: the courtyard tint of
    edge-aligned parts (own constraints only) and the line through a line group's members."""
    if not reference:
        fill = _mix(theme.SUBSTRATE, theme.CONSTRAINT, 0.13)
        for ref, _edge, _tol in edge_refs(constraints):
            if ref in poses and ref in renderer.components:
                box = renderer._courtyard_box(tf, ref, poses[ref])
                draw.rectangle(box, fill=fill)
    target = _rgb(theme.REFERENCE if reference else theme.CONSTRAINT)
    for _name, refs in line_members(constraints):
        pixels = [tf(*poses[r][:2]) for r in refs if r in poses]
        if len(pixels) < 2:
            continue
        if reference:
            for a, b in zip(pixels, pixels[1:]):
                renderer._dashed(draw, a, b, max(1, int(1.5 * ss)), target, tf, ss)
        else:
            draw.line(pixels, fill=target, width=max(1, int(1.5 * ss)))


def _edge_segment(header, edge):
    w, h = header["outline"]["w"], header["outline"]["h"]
    return {
        "south": ((0, 0), (w, 0)),
        "north": ((0, h), (w, h)),
        "west": ((0, 0), (0, h)),
        "east": ((w, 0), (w, h)),
    }[edge]


def draw_over(renderer, draw, tf, poses, constraints, reference, ss):
    """Lines, rigid bodies, target edges and tethers, over everything but the overlay."""
    header = renderer.header
    target = _rgb(theme.REFERENCE if reference else theme.CONSTRAINT)
    for edge in sorted({e for _r, e, _t in edge_refs(constraints)}, key=EDGES.index):
        a, b = _edge_segment(header, edge)
        if reference:
            renderer._dashed(draw, tf(*a), tf(*b), 2 * ss, target, tf, ss)
        else:
            draw.line([tf(*a), tf(*b)], fill=target, width=max(1, 3 * ss))
    for ref, edge, tol in edge_refs(constraints) if not reference else ():
        if ref not in poses or ref not in renderer.components:
            continue
        comp = renderer.components[ref]
        x, y, rot, _side = poses[ref]
        w, h = extent(comp, rot)
        d = edge_distance(header, comp, poses[ref], edge)
        if edge == "south":
            a, b = (x, y - h / 2.0), (x, 0)
        elif edge == "north":
            a, b = (x, y + h / 2.0), (x, header["outline"]["h"])
        elif edge == "west":
            a, b = (x - w / 2.0, y), (0, y)
        else:
            a, b = (x + w / 2.0, y), (header["outline"]["w"], y)
        ok = d <= tol + 1.0
        color = _rgb(theme.CONSTRAINT_OK) if ok else _rgb(theme.CONSTRAINT_BAD)
        pa, pb = tf(*a), tf(*b)
        if math.dist(pa, pb) >= 1.0:
            draw.line([pa, pb], fill=color, width=max(1, int(1.5 * ss)))
        r = max(2.0 * ss, 1.0)
        draw.ellipse((pb[0] - r, pb[1] - r, pb[0] + r, pb[1] + r), fill=color)
        box = renderer._courtyard_box(tf, ref, poses[ref])
        draw.rectangle(box, outline=color, width=max(1, int(1.5 * ss)))
    if not reference:
        for _name, refs in line_members(constraints):
            if len([r for r in refs if r in poses]) == len(refs) and len(refs) >= 2:
                _rigid_box(renderer, draw, tf, poses, refs, target, ss)


def _rigid_box(renderer, draw, tf, poses, refs, color, ss, margin=350):
    """A dashed rectangle around the members' courtyards, aligned with the line D1 to Dn."""
    (x0, y0), (x1, y1) = poses[refs[0]][:2], poses[refs[-1]][:2]
    angle = math.atan2(y1 - y0, x1 - x0) if (x0, y0) != (x1, y1) else 0.0
    ux, uy = math.cos(angle), math.sin(angle)
    vx, vy = -uy, ux
    us, vs = [], []
    for ref in refs:
        comp = renderer.components.get(ref)
        if comp is None:
            continue
        x, y, rot, _side = poses[ref]
        w, h = extent(comp, rot)
        for cx, cy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            px, py = x + cx, y + cy
            us.append(px * ux + py * uy)
            vs.append(px * vx + py * vy)
    if not us:
        return
    u0, u1, v0, v1 = min(us) - margin, max(us) + margin, min(vs) - margin, max(vs) + margin
    corners = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
    points = [tf(u * ux + v * vx, u * uy + v * vy) for u, v in corners]
    for a, b in zip(points, points[1:] + points[:1]):
        renderer._dashed(draw, a, b, max(1, int(1.2 * ss)), color, tf, ss)
