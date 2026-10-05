"""D15 ground-stitching variants of the L1 pour (owner 2026-10-04) and the L2-L3 plane pair.

- A "grid" and B "sparse": the pour rectangles as before (north and east strips, the launch
  ground under the package); B differs only in the via lattice (`rfmacro.vias`).
- C "strips": no L1 pour in the antenna area except
  - the GCPW ground strips of every feed and dummy run-in (the line's corridor out to the fence
    row's via pads plus `strip_margin`: per segment a rectangle or an annular sector, so every
    zone is a simple polygon KiCad fills as it is),
  - the rings round the cut-outs (out to the guard-band row's pads plus `strip_margin`; the
    2.0 mm isolation strip between the banks lies inside them) and the isolation wall south of
    them,
  - the load cells (via zone plus pads), and
  - the launch ground under the package (unchanged).
  Elsewhere L1 is bare; the L2-L3 lattice vias (`l23_pitch`) keep their L1 pads only.

The L2-L3 check: every point of the plane pair outside the package, the cut-outs and the PA
island (its anti-pads perforate L2/L3 and its vias do not stitch them) within `l23_dmax` of a GND
through via (straight line: L2 and L3 are solid planes).
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .geom import Path
from .params import RULES

Pt = Tuple[float, float]
Rect = Tuple[float, float, float, float]
C0 = 299792458.0


def _poly_rect(r: Rect) -> List[Pt]:
    return [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]


def clip(poly: List[Pt], r: Rect) -> List[Pt]:
    """Sutherland-Hodgman clip of a polygon to an axis-aligned rectangle."""

    def cut(pts, inside, inter):
        out = []
        n = len(pts)
        for i in range(n):
            a, b = pts[i - 1], pts[i]
            ia, ib = inside(a), inside(b)
            if ib:
                if not ia:
                    out.append(inter(a, b))
                out.append(b)
            elif ia:
                out.append(inter(a, b))
        return out

    def ix(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))

    def iy(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)

    pts = list(poly)
    for inside, inter in (
        (lambda q: q[0] >= r[0], ix(r[0])),
        (lambda q: q[0] <= r[2], ix(r[2])),
        (lambda q: q[1] >= r[1], iy(r[1])),
        (lambda q: q[1] <= r[3], iy(r[3])),
    ):
        if not pts:
            break
        pts = cut(pts, inside, inter)
    return pts


def _area(poly: List[Pt]) -> float:
    return 0.5 * abs(
        sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly)))
    )


def corridor_pieces(path: Path, hw: float, step_deg: float = 6.0) -> List[List[Pt]]:
    """The corridor of half-width `hw` round a path, one simple polygon per segment: a rotated
    rectangle for a straight, an annular sector (a sector from the centre when the radius is
    under hw) for an arc."""
    out = []
    for sg in path.segs:
        if sg.kind == "line":
            (x0, y0), (x1, y1) = sg.p0, sg.p1
            L = math.dist(sg.p0, sg.p1)
            if L < 1e-9:
                continue
            nx, ny = -(y1 - y0) / L * hw, (x1 - x0) / L * hw
            out.append(
                [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
            )
            continue
        cx, cy = sg.center
        r_out, r_in = sg.radius + hw, max(0.0, sg.radius - hw)
        n = max(2, int(math.ceil(abs(math.degrees(sg.sweep)) / step_deg)))
        angs = [sg.a0 + sg.sweep * k / n for k in range(n + 1)]
        # the outer arc's chords stay outside the true arc (radius r_out / cos(half step))
        ro = r_out / math.cos(abs(sg.sweep) / n / 2)
        outer = [(cx + ro * math.cos(a), cy + ro * math.sin(a)) for a in angs]
        if r_in > 1e-9:
            inner = [(cx + r_in * math.cos(a), cy + r_in * math.sin(a)) for a in reversed(angs)]
        else:
            inner = [(cx, cy)]
        out.append(outer + inner)
    return out


def zones(mc, ru) -> List[List[Pt]]:
    """L1 GND zones of the macro for its D15 pour mode."""
    p = mc.params
    if p["pour_mode"] != "strips":
        return [list(q) for q in mc.pour]
    gpr = RULES["via_fence"][1] / 2
    m = float(p["strip_margin"])
    hw = ru.foff + gpr + m
    north, east = (_bbox(mc.pour[0]), _bbox(mc.pour[1]))
    pieces: List[List[Pt]] = []
    for f in list(mc.feeds.values()) + list(mc.runins.values()):
        pieces += corridor_pieces(f, hw)
    for cut in mc.cutouts.values():
        g = ru.B + gpr + m
        pieces.append(_poly_rect((cut[0] - g, cut[1] - g, cut[2] + g, cut[3] + g)))
    for ld in mc.loads.values():
        z = ld.via_zone
        g = gpr + m
        pieces.append(_poly_rect((z[0] - g, z[1] - g, z[2] + g, z[3] + g)))
    x_iso = mc.ports["iso_fence_x"]["at"][0]
    y_top = max(c[3] for c in mc.cutouts.values()) + ru.B
    pieces.append(
        _poly_rect((x_iso - gpr - m, ru.half + 0.6 - gpr - m, x_iso + gpr + m, y_top + gpr + m))
    )
    out = []
    for pc in pieces:
        for box in (north, east):
            c = clip(pc, box)
            if len(c) >= 3 and _area(c) > 1e-4:
                out.append([(round(x, 5), round(y, 5)) for x, y in c])
    out += [list(q) for q in mc.pour[2:]]  # the launch ground under the package
    return out


def _bbox(poly) -> Rect:
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    return (min(xs), min(ys), max(xs), max(ys))


# ---- L2-L3 plane pair --------------------------------------------------------------------


def l23_domain(mc, ru, g):
    """The L2-L3 plane pair the macro stitches with its open lattice: the region outside the
    package (and its fence-start band), minus the cut-outs (the bank cavities: G9), the feed
    corridors (walled by their fence rows, G4: a line between shared rows is up to 0.72 mm from
    them) and the PA island (copper + clearance + pad radius)."""
    from .rules import _pts

    m = g.empty()
    for poly in mc.pour[:2]:
        g.polygon(m, poly)
    cut = g.empty()
    for c in mc.cutouts.values():
        g.rect(cut, *c)
    h = ru.half + ru.fence_start
    g.rect(cut, -h, -h, h, h)
    for path in list(mc.feeds.values()) + list(mc.runins.values()):
        g.capsules(cut, _pts(path), ru.foff + 0.25)
    if getattr(mc, "pa", None) is not None:
        from .pa import keepouts

        clr = float(mc.params["pa_clear"]) + RULES["via_fence"][1] / 2
        for r in keepouts(mc.pa, clr):
            g.rect(cut, *r)
    return g.andnot(m, cut)


def l23_raster(mc, ru, h: float = 0.01) -> Dict[str, object]:
    """Pieces of the L2-L3 domain farther than l23_dmax from every GND via."""
    from .raster import Grid

    x0, x1 = mc.region["x"]
    y0, y1 = mc.region["y"]
    g = Grid(x0, y0, x1, y1, h)
    dom = l23_domain(mc, ru, g)
    cov = g.empty()
    g.disks(cov, [v[0] for v in mc.vias], float(mc.params["l23_dmax"]))
    bad = g.andnot(dom, cov)
    pieces = []
    for comp in g.components(bad):
        if len(comp) < 4:
            continue
        dsc = g.describe(comp)
        cx, cy = dsc["at"]
        i, j = min(comp, key=lambda t: (g.xc(t[0]) - cx) ** 2 + (g.yc(t[1]) - cy) ** 2)
        dsc["near"] = [g.xc(i), g.yc(j)]
        pieces.append(dsc)
    pieces.sort(key=lambda t: -t["area_mm2"])
    return dict(domain_mm2=round(g.area(dom), 3), pieces=pieces)


def largest_gap(mc, ru, h: float = 0.02) -> Dict[str, object]:
    """[D] The largest via-free circle of the L2-L3 domain (radius = the largest distance from a
    domain point to a GND via), by bisection on disks."""
    from .raster import Grid

    x0, x1 = mc.region["x"]
    y0, y1 = mc.region["y"]
    g = Grid(x0, y0, x1, y1, h)
    dom = l23_domain(mc, ru, g)
    pts = [v[0] for v in mc.vias]
    lo, hi = 0.0, 3.0
    for _ in range(9):
        r = 0.5 * (lo + hi)
        cov = g.empty()
        g.disks(cov, pts, r)
        if g.count(g.andnot(dom, cov)):
            lo = r
        else:
            hi = r
    cov = g.empty()
    g.disks(cov, pts, lo)
    left = g.andnot(dom, cov)
    at = None
    for j, row in enumerate(left):
        if row:
            i = (row & -row).bit_length() - 1
            at = [round(g.xc(i), 3), round(g.yc(j), 3)]
            break
    return dict(radius_mm=round(hi, 3), at=at)


def lattice_cutoff_ghz(pitch: float, drill: float, er: float) -> float:
    """[D] Parallel-plate cut-off of a square via lattice of `pitch` as two post walls (SIW
    equivalent width W - 1.08 d^2/s + 0.1 d^2/W with W = s = pitch): c / (2 W_eff sqrt(er))."""
    w = pitch - 1.08 * drill**2 / pitch + 0.1 * drill**2 / pitch
    return C0 / (2 * w * 1e-3 * math.sqrt(er)) / 1e9


def span_cutoff_ghz(radius: float, drill: float, er: float) -> float:
    """[D] The same bound for the largest via-free circle: a span of 2 r - d between posts."""
    w = max(1e-3, 2 * radius - drill)
    return C0 / (2 * w * 1e-3 * math.sqrt(er)) / 1e9
