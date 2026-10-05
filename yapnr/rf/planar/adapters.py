"""Adapters into yapnr-planar-v1: generated lines, the radar60 macro's patch, the radar60 feed
models, and a region of a zone-filled KiCad board.

- ``line_model``: a straight microstrip or fenced GCPW between two wave ports (validation case
  (a) of the Palace plan).
- ``patch_from_record``: the single inset-fed patch on its L2 window from an ``rfmacro`` record
  (schema ``radar60-rfmacro/1``), as the openEMS single-patch diagnostic builds it, with the board
  either running into the south wall under the feed (``w``: a wave port) or finite with a lumped
  port (``finite``).
- ``from_feedmodel``: the JSON that the radar60 RF-uniformity ``prep.py`` cut from the zone-filled
  macro board for openEMS (``tx12-*``, ``col12-*``): the very geometry those runs used.
- ``from_kicad``: copper of a zone-filled ``.kicad_pcb`` inside a box, read with yapnr's own
  S-expression reader (no pcbnew): zone fills, tracks and arcs, vias with their pads, and
  footprint pads (rect, roundrect, circle, oval, and custom pads drawn with gr_poly, gr_rect or
  gr_circle primitives: BGA lands and the radar60 patches; other shapes are listed in
  ``provenance.pads_skipped``). Plated holes of through-hole pads become via barrels.

Every adapter returns a document that ``model.check`` accepts; the shapely-based ones clean the
copper (``clean.clean_geometry``) and record what the cleaning moved in ``provenance``.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from yapnr.rf.planar import clean, model, stackups

C0 = 299792458.0


def _rect(x0: float, y0: float, x1: float, y1: float) -> List[List[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _poly(outer, holes=()) -> Dict[str, Any]:
    return dict(
        outer=[list(map(float, p)) for p in outer],
        holes=[[list(map(float, p)) for p in h] for h in holes],
    )


def _floor(floor: str) -> Dict[str, str]:
    if floor not in ("metal", "pec"):
        raise ValueError(f"floor is 'metal' or 'pec', not {floor!r}")
    return {"zmin": floor}


def _floor_metal(doc: Dict[str, Any], floor: str, metal: Optional[Dict[str, Any]] = None) -> None:
    """A ``metal`` floor's conductor: the feed stack's L2 unless given."""
    if floor == "metal":
        doc["domain"]["metal"] = {"zmin": dict(metal or stackups.floor())}


# --- generated lines ---------------------------------------------------------------------------


def line_model(
    kind: str = "msl",
    length: float = 5.0,
    lead: float = 0.5,
    w: float = 0.2,
    gap: float = 0.2,
    half_width: float = 1.5,
    air: float = 1.3,
    fence_offset: float = 0.5,
    fence_pitch: float = 0.45,
    drill: float = 0.15,
    l1_model: str = "sheet",
    floor: str = "metal",
) -> Dict[str, Any]:
    """A straight 50-ohm line on the radar60 feed stack (RO4835 4 mil over the L2 floor: a lossy
    ``metal`` wall, the default, or ``pec`` as the openEMS feed models have it).

    The line runs wall to wall along x; wave ports P1 (west) and P2 (east) sit ``lead`` inside the
    walls, so the de-embedded line between them is ``length`` long. ``gcpw`` adds GND copper from
    the gap edges to the side walls and a via fence on each side (offset 0.5, pitch 0.45, drill
    0.15: the macro's fence rule), kept a pitch away from the end walls.
    """
    diel, layers = stackups.radar60("feed", l1_model=l1_model)
    h = diel[0]["z1"]
    total = length + 2 * lead
    doc = model.new(
        f"line-{kind}-{length:g}mm",
        [0.0, total, -half_width, half_width, 0.0, round(h + air, 6)],
        _floor(floor),
    )
    _floor_metal(doc, floor)
    doc["stack"] = dict(dielectrics=diel, layers=layers)
    doc["conductors"].append(
        dict(layer="L1", net="SIG", polygons=[_poly(_rect(0.0, -w / 2, total, w / 2))])
    )
    if kind == "gcpw":
        e = w / 2 + gap
        doc["conductors"].append(
            dict(
                layer="L1",
                net="GND",
                polygons=[
                    _poly(_rect(0.0, e, total, half_width)),
                    _poly(_rect(0.0, -half_width, total, -e)),
                ],
            )
        )
        x = fence_pitch
        while x <= total - fence_pitch + 1e-9:
            for y in (fence_offset, -fence_offset):
                doc["vias"].append(
                    dict(
                        at=[round(x, 6), y], drill=drill, **{"from": "zmin", "to": "L1"}, net="GND"
                    )
                )
            x += fence_pitch
    elif kind != "msl":
        raise ValueError(f"unknown line kind {kind!r}")
    for name, x, d in (("P1", lead, "+x"), ("P2", total - lead, "-x")):
        doc["ports"].append(
            dict(
                name=name,
                kind="wave",
                net="SIG",
                layer="L1",
                ref="zmin",
                at=[x, 0.0],
                dir=d,
                width=w,
                z0=50.0,
                excite=name == "P1",
            )
        )
    doc["mesh"] = dict(f_max_ghz=70.0)
    doc["provenance"] = dict(
        source="generated",
        generator="yapnr.rf.planar.adapters.line_model",
        kind=kind,
        length=length,
    )
    return model.check(doc)


# --- the radar60 patch -------------------------------------------------------------------------


def patch_from_record(
    record: Dict[str, Any],
    variant: str = "w",
    lead: float = 1.0,
    p1_gap: float = 0.6,
    lumped_lead: float = 1.2,
    board_margin_l0: float = 0.5,
    air_l0: float = 0.3,
    below_l0: float = 0.2,
    l1_model: str = "sheet",
) -> Dict[str, Any]:
    """The upper patch of a column alone, inset-fed from the south, on its L2 window.

    Geometry as ``build_column`` and the openEMS single-patch run (``column_sim.py --single``):
    patch W x L centred at (0, spacing/2) with an inset notch (``inset`` deep, ``w50/2 + notch``
    half wide), a ``w50`` line from the reference plane P1 (``p1_gap`` south of the patch edge) to
    the notch bottom (+0.02 overlap), the L2 window ``window_margin`` beyond the patch, and the
    board (RO4450F, L3, L2, RO4835) ``board_margin_l0`` x lambda0 beyond the copper.

    ``w``: the board, L2 and L3 run south into the domain wall under the feed, where a wave port
    de-embeds ``lead`` to P1 (the comparison geometry for both solvers). ``finite``: the finite
    board of the stage-2 run with a 50-ohm lumped port across the core ``lumped_lead`` south of P1
    (the tie-back to stage 2; its S11 is referenced at the lumped port). Air reaches
    ``air_l0`` x lambda0 beyond the board and above L1, ``below_l0`` x lambda0 under L3.
    """
    p, d = record["params"], record["dims"]
    W, L, ins = float(d["patch"]["w"]), float(d["patch"]["l"]), float(d["patch"]["inset"])
    notch, w50, m = float(p["notch"]), float(p["w50"]), float(p["window_margin"])
    s = float(p["spacing"])
    lam0 = float(d["lambda0_mm"])
    yc = s / 2
    edge = yc - L / 2
    p1y = edge - p1_gap
    nw = w50 / 2 + notch
    patch = [[-W / 2, edge], [-nw, edge], [-nw, edge + ins], [nw, edge + ins], [nw, edge], [W / 2, edge],
             [W / 2, edge + L], [-W / 2, edge + L]]  # fmt: skip
    diel, layers = stackups.radar60("window", l1_model=l1_model)
    z_l1 = layers[-1]["z"]
    mb = board_margin_l0 * lam0
    air = air_l0 * lam0
    bx0, bx1, by1 = -W / 2 - mb, W / 2 + mb, edge + L + mb
    if variant == "w":
        y_wall = p1y - lead
        by0 = y_wall
        line_y0 = y_wall
        box = [bx0 - air, bx1 + air, y_wall, by1 + air, -below_l0 * lam0, z_l1 + air]
    elif variant == "finite":
        line_y0 = p1y - lumped_lead
        by0 = min(line_y0, edge) - mb
        box = [bx0 - air, bx1 + air, by0 - air, by1 + air, -below_l0 * lam0, z_l1 + air]
    else:
        raise ValueError(f"unknown patch variant {variant!r}")
    box = [round(v, 6) for v in box]
    doc = model.new(f"patch-{variant}", box)
    outline = _rect(bx0, by0, bx1, by1)
    for dd in diel:
        dd["outline"] = outline
    doc["stack"] = dict(dielectrics=diel, layers=layers)
    win = _rect(-W / 2 - m, edge - m, W / 2 + m, edge + L + m)
    geometry, ops = clean._shapely()
    rf = ops.unary_union(
        [geometry.Polygon(patch), geometry.box(-w50 / 2, line_y0, w50 / 2, edge + ins + 0.02)]
    )
    doc["conductors"] = [
        dict(layer="L1", net="RF", polygons=clean.clean_geometry(rf, refit=False)),
        dict(layer="L2", net="GND", polygons=[_poly(outline, [list(reversed(win))])]),
        dict(layer="L3", net="GND", polygons=[_poly(outline)]),
    ]
    if variant == "w":
        doc["ports"] = [
            dict(
                name="P1",
                kind="wave",
                net="RF",
                layer="L1",
                ref="L2",
                at=[0.0, p1y],
                dir="+y",
                width=w50,
                z0=50.0,
                excite=True,
            )
        ]
    else:
        doc["ports"] = [
            dict(
                name="P1",
                kind="lumped",
                net="RF",
                layer="L1",
                ref="L2",
                at=[0.0, line_y0],
                dir="+y",
                width=w50,
                z0=50.0,
                excite=True,
            )
        ]
    doc["features"] = [
        dict(name="patch", bounds=[-W / 2, edge, W / 2, edge + L]),
        dict(name="window", bounds=[win[0][0], win[0][1], win[2][0], win[2][1]]),
    ]
    doc["mesh"] = dict(f_max_ghz=70.0)
    doc["provenance"] = dict(
        source=f"rfmacro record ({record.get('schema')}, geometry {str(record.get('geometry_sha256', ''))[:12]})",
        generator="yapnr.rf.planar.adapters.patch_from_record",
        patch=dict(w=W, l=L, inset=ins, notch=notch, w50=w50, window_margin=m, spacing=s, p1_y=p1y),
        variant=variant,
        reference_plane="P1, %.3f mm south of the patch edge" % p1_gap,
    )
    return model.check(doc)


# --- the radar60 feed models (prep.py) ---------------------------------------------------------


def _union_pieces(pieces: Iterable[Sequence[Sequence[float]]], eps: float = 2e-4):
    """Union of hole-free pieces cut along straight seams (prep.py splits polygons with holes and
    simplifies each piece): grow by ``eps``, union, shrink back (mitre joins keep the corners)."""
    geometry, ops = clean._shapely()
    polys = [geometry.Polygon(p).buffer(0) for p in pieces if len(p) >= 3]
    u = ops.unary_union([q.buffer(eps, join_style="mitre") for q in polys if not q.is_empty])
    return u.buffer(-eps, join_style="mitre")


def fit_box(
    box: Sequence[float],
    vias: Iterable[Sequence[float]],
    points: Iterable[Sequence[float]] = (),
    clear: float = 0.05,
    gap: float = 0.05,
    step: float = 0.005,
    max_move: float = 0.5,
) -> Tuple[List[float], List[str]]:
    """Nudge the x/y walls of ``box`` so that no wall cuts a via barrel or leaves a sliver.

    Every via barrel (x, y, drill) must end up wholly outside a wall or at least ``clear``
    inside it, and no copper vertex in ``points`` may lie within ``gap`` of a wall without being
    on it. Each wall takes the smallest outward move (in ``step``, at most ``max_move``) that
    satisfies this, or else the smallest inward one: outward only adds stitched pour beyond the
    fences, inward could cut a line's ground. The walls are revisited until none moves. Returns
    the box and a note per moved wall.
    """
    b = [float(v) for v in box]
    start = list(b)
    vias = [(float(v[0]), float(v[1]), 0.5 * float(v[2])) for v in vias]
    pts = [(float(p[0]), float(p[1])) for p in points]
    walls = ((0, -1), (0, 1), (1, -1), (1, 1))

    def ok(k: int, w: float) -> bool:
        axis, sign = walls[k]
        lo, hi = (b[2], b[3]) if axis == 0 else (b[0], b[1])
        for x, y, r in vias:
            c, other = (x, y) if axis == 0 else (y, x)
            if not (lo - r <= other <= hi + r):
                continue
            inside = (c - w) if sign < 0 else (w - c)
            if -r < inside < r + clear:
                return False
        for x, y in pts:
            c, other = (x, y) if axis == 0 else (y, x)
            if lo <= other <= hi and 1e-9 < abs(c - w) < gap:
                return False
        return True

    for _ in range(20):
        changed = False
        for k in range(4):
            if ok(k, b[k]):
                continue
            n = int(max_move / step)
            cand = [b[k] + walls[k][1] * i * step for i in range(1, n + 1)]
            cand += [b[k] - walls[k][1] * i * step for i in range(1, n + 1)]
            found = next((w for w in cand if ok(k, w)), None)
            if found is None:
                raise ValueError(
                    f"no wall position within {max_move} mm clears the vias and copper"
                )
            b[k] = found
            changed = True
        if not changed:
            break
    names = ("xmin", "xmax", "ymin", "ymax")
    notes = [
        f"{names[k]} moved {b[k] - start[k]:+.3f} mm (via barrels, copper slivers)"
        for k in range(4)
        if abs(b[k] - start[k]) > 1e-9
    ]
    return [round(v, 6) for v in b], notes


def from_feedmodel(
    fm: Dict[str, Any],
    box: Optional[Sequence[float]] = None,
    air: float = 1.306,
    w50: float = 0.2,
    l1_model: str = "sheet",
    refit: bool = True,
    name: Optional[str] = None,
    source: str = "",
    floor: str = "metal",
) -> Dict[str, Any]:
    """A radar60 feed model (prep.py output) as a planar document.

    ``box`` [x0, x1, y0, y1] is the Palace domain (default: prep.py's ``inner`` box, where the
    openEMS copper is the conducting sheet). The copper is re-unioned from prep.py's hole-free
    pieces, cropped to the box and cleaned; vias whose centre is outside the box are dropped (the
    box should be chosen with ``fit_box`` so that no wall cuts a barrel). Ports keep prep.py's
    planes: ``P0`` lines head east (``+x``), ``P1`` lines north out of the box (``-y`` into it).
    L2 is the floor (feed models have no windows): a lossy ``metal`` wall by default, ``pec`` as
    the openEMS runs had it; ``air`` is the air above L1 (1.306 mm: up to the openEMS PML of those
    runs).
    """
    geometry, ops = clean._shapely()
    if fm.get("l2_windows"):
        raise ValueError(
            "feed models with L2 windows (col12) need the window stack: not supported yet"
        )
    bx = (
        list(box)
        if box is not None
        else [fm["inner"][0], fm["inner"][2], fm["inner"][1], fm["inner"][3]]
    )
    diel, layers = stackups.radar60("feed", l1_model=l1_model)
    z1 = round(diel[0]["z1"] + air, 6)
    doc = model.new(
        name or f"{fm['model']}-{fm['variant']}", [*map(float, bx), 0.0, z1], _floor(floor)
    )
    _floor_metal(doc, floor)
    doc["stack"] = dict(dielectrics=diel, layers=layers)
    crop = geometry.box(bx[0], bx[2], bx[1], bx[3])
    nets = {"GND": fm["gnd"]}
    nets.update(fm["nets"])
    moved = {}
    for net, (inner, zone) in nets.items():
        g = _union_pieces(list(inner) + list(zone)).intersection(crop)
        polys = clean.clean_geometry(g, refit=refit)
        if polys:
            doc["conductors"].append(dict(layer="L1", net=net, polygons=polys))
            moved[net] = round(clean.cleaning_change(g, polys), 6)
    for v in fm["vias"]:
        x, y, drill = float(v[0]), float(v[1]), float(v[2])
        if bx[0] < x < bx[1] and bx[2] < y < bx[3]:
            doc["vias"].append(
                dict(at=[x, y], drill=drill, **{"from": "zmin", "to": "L1"}, net="GND")
            )
    for p in fm["ports"]:
        d = "+x" if p["dir"] == "x" else "-y"
        doc["ports"].append(
            dict(
                name=p["name"],
                kind="wave",
                net=p["net"],
                layer="L1",
                ref="zmin",
                at=[float(p["at"][0]), float(p["at"][1])],
                dir=d,
                width=w50,
                z0=50.0,
                excite=False,
            )
        )
    doc["features"] = [
        dict(name=f"sliver{k}", bounds=s["bounds"], area=s["area"], centroid=s["centroid"])
        for k, s in enumerate(fm.get("slivers", []))
    ]
    doc["mesh"] = dict(f_max_ghz=70.0)
    doc["provenance"] = dict(
        source=source or "radar60 rf-uniform prep.py model",
        generator="yapnr.rf.planar.adapters.from_feedmodel",
        model=fm["model"],
        variant=fm["variant"],
        openems_box=fm["box"],
        openems_inner=fm["inner"],
        cleaning_xor_mm2=moved,
    )
    return model.check(doc)


# --- KiCad -------------------------------------------------------------------------------------


def _pts(node, frame) -> List[Tuple[float, float]]:
    from yapnr.fab.board import children

    out = []
    pts = next(iter(children(node, "pts")), None)
    for item in pts[1:] if pts else []:
        if isinstance(item, list) and item and item[0] == "xy":
            out.append(_xf(float(item[1]), float(item[2]), frame))
        elif isinstance(item, list) and item and item[0] == "arc":
            a = _xf(*_xy(item, "start"), frame)
            mm = _xf(*_xy(item, "mid"), frame)
            b = _xf(*_xy(item, "end"), frame)
            out.extend(model.arc_points(a, mm, b, chord=0.002))
            out.append(b)
    return out


def _xy(node, name) -> Tuple[float, float]:
    from yapnr.fab.board import child

    c = child(node, name)
    return float(c[1]), float(c[2])


def _xf(x: float, y: float, frame) -> Tuple[float, float]:
    x0, y0, flip = frame
    return (x - x0, (y0 - y) if flip else (y - y0))


def _net_name(node) -> Optional[str]:
    """A ``(net ...)`` child's name: ``(net "GND")`` (KiCad 10) or ``(net 3 "GND")`` (older)."""
    from yapnr.fab.board import child

    n = child(node, "net")
    if n is None or len(n) < 2:
        return None
    return str(n[2]) if len(n) >= 3 else str(n[1])


def _rotate(u: float, v: float, angle_deg: float) -> Tuple[float, float]:
    """A pad-local offset turned by KiCad's angle (counter-clockwise on screen, y down)."""
    a = math.radians(angle_deg)
    return (u * math.cos(a) + v * math.sin(a), -u * math.sin(a) + v * math.cos(a))


def _pad_local_shapes(pad, geometry) -> Tuple[List[Any], Optional[str]]:
    """The pad's copper in pad-local coordinates (unrotated, KiCad units and y-down), or the
    reason it is skipped."""
    from yapnr.fab.board import child, children, value

    shape = str(pad[3]) if len(pad) > 3 else ""
    size = child(pad, "size")
    sx, sy = (float(size[1]), float(size[2])) if size is not None and len(size) >= 3 else (0, 0)
    if shape == "rect":
        return [geometry.box(-sx / 2, -sy / 2, sx / 2, sy / 2)], None
    if shape == "roundrect":
        r = float(value(pad, "roundrect_rratio", 0.25)) * min(sx, sy)
        core = geometry.box(-sx / 2 + r, -sy / 2 + r, sx / 2 - r, sy / 2 - r)
        return [core.buffer(r, quad_segs=16) if r > 0 else core], None
    if shape == "circle":
        return [geometry.Point(0.0, 0.0).buffer(sx / 2, quad_segs=32)], None
    if shape == "oval":
        r = min(sx, sy) / 2
        a = (sx / 2 - r, 0.0) if sx >= sy else (0.0, sy / 2 - r)
        line = geometry.LineString([(-a[0], -a[1]), (a[0], a[1])])
        return [line.buffer(r, quad_segs=32) if line.length > 0 else line.centroid.buffer(r)], None
    if shape != "custom":
        return [], f"pad shape {shape}"
    out = []
    opts = child(pad, "options")
    anchor = value(opts, "anchor", "rect") if opts is not None else "rect"
    if anchor == "circle":
        out.append(geometry.Point(0.0, 0.0).buffer(sx / 2, quad_segs=32))
    else:
        out.append(geometry.box(-sx / 2, -sy / 2, sx / 2, sy / 2))
    prims = child(pad, "primitives")
    for prim in prims[1:] if prims is not None else []:
        if not isinstance(prim, list) or not prim:
            continue
        kind = str(prim[0])
        width = float(value(prim, "width", 0.0) or 0.0)
        if kind == "gr_poly":
            pts = [
                (float(q[1]), float(q[2]))
                for q in next(iter(children(prim, "pts")), [])[1:]
                if isinstance(q, list) and q and q[0] == "xy"
            ]
            if len(pts) >= 3:
                g = geometry.Polygon(pts).buffer(0)
                out.append(g.buffer(width / 2, quad_segs=16) if width > 0 else g)
        elif kind == "gr_rect":
            (ax, ay), (bx, by) = (
                (float(child(prim, k)[1]), float(child(prim, k)[2])) for k in ("start", "end")
            )
            g = geometry.box(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
            out.append(g.buffer(width / 2, join_style="mitre") if width > 0 else g)
        elif kind == "gr_circle":
            c, e = child(prim, "center"), child(prim, "end")
            r = math.hypot(float(e[1]) - float(c[1]), float(e[2]) - float(c[2]))
            out.append(geometry.Point(float(c[1]), float(c[2])).buffer(r + width / 2, quad_segs=32))
        else:
            return [], f"custom pad primitive {kind}"
    return out, None


def read_kicad_pads(
    tree, layers: Dict[str, str], frame, keep: Optional[set], geometry
) -> Tuple[List[Tuple[str, str, Any]], List[Dict[str, Any]], List[str]]:
    """Footprint pads of a parsed board: ([(planar layer, net, geometry)], plated holes as vias,
    skipped pads). A pad's ``(at x y angle)`` is footprint-relative in position and absolute in
    angle (KiCad's file format); its layers ``*.Cu`` (or ``F&B.Cu``) mean every copper layer."""
    from shapely import affinity

    from yapnr.fab.board import child, children

    shapes: List[Tuple[str, str, Any]] = []
    holes: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for fp in children(tree, "footprint"):
        fat = child(fp, "at")
        fx, fy = float(fat[1]), float(fat[2])
        frot = float(fat[3]) if len(fat) > 3 else 0.0
        ref = next(
            (
                str(p[2])
                for p in children(fp, "property")
                if len(p) >= 3 and str(p[1]) == "Reference"
            ),
            "?",
        )
        for pad in children(fp, "pad"):
            net = _net_name(pad)
            if keep is not None and net not in keep:
                continue
            lnode = child(pad, "layers")
            names = [str(x) for x in lnode[1:]] if lnode is not None else []
            mapped = [
                la
                for la in layers
                if la in names or "*.Cu" in names or ("F&B.Cu" in names and la in ("F.Cu", "B.Cu"))
            ]
            if not mapped:
                continue
            pat = child(pad, "at")
            pu, pv = float(pat[1]), float(pat[2])
            pang = float(pat[3]) if len(pat) > 3 else 0.0
            du, dv = _rotate(pu, pv, frot)
            cx, cy = fx + du, fy + dv  # the pad's centre on the board (KiCad coordinates)
            local, why = _pad_local_shapes(pad, geometry)
            if why:
                skipped.append(f"{ref}.{pad[1] if len(pad) > 1 else '?'}: {why}")
                continue
            a = math.radians(pang)
            for g in local:
                # pad-local (u, v) -> board (cx + u cos a + v sin a, cy - u sin a + v cos a) ->
                # planar frame (x - x0, y0 - y when flipped)
                g = affinity.affine_transform(
                    g, [math.cos(a), math.sin(a), -math.sin(a), math.cos(a), cx, cy]
                )
                x0, y0, flip = frame
                g = affinity.affine_transform(
                    g, [1.0, 0.0, 0.0, -1.0, -x0, y0] if flip else [1.0, 0.0, 0.0, 1.0, -x0, -y0]
                )
                for la in mapped:
                    shapes.append((la, net or "", g))
            drill = child(pad, "drill")
            if str(pad[2]) == "thru_hole" and drill is not None:
                nums = [float(x) for x in drill[1:] if not isinstance(x, list) and x != "oval"]
                if nums:
                    holes.append(
                        dict(
                            at=list(_xf(cx, cy, frame)),
                            drill=nums[0],
                            size=0.0,
                            net=net or "",
                            layers=["F.Cu", "B.Cu"],
                        )
                    )
    return shapes, holes, skipped


def read_kicad_copper(
    text: str,
    layers: Dict[str, str],
    frame=(0.0, 0.0, True),
    nets: Optional[Iterable[str]] = None,
    arc_chord: float = 0.002,
    pads: bool = True,
) -> Dict[str, Any]:
    """Raw copper of a zone-filled board: shapely geometry per (layer, net), the vias (and the
    plated holes of through-hole pads) and the pads that were skipped.

    ``layers`` maps KiCad layer names to planar layer names (``{"F.Cu": "L1"}``); ``frame`` is
    (x0, y0, flip_y): planar x = kx - x0 and y = y0 - ky when flipped (KiCad's y points down).
    """
    from yapnr.fab.board import child, children, parse, value

    geometry, ops = clean._shapely()
    tree = parse(text)
    keep = set(nets) if nets is not None else None
    shapes: Dict[Tuple[str, str], List[Any]] = {}

    def add(lay, net, g):
        if lay in layers and (keep is None or net in keep):
            shapes.setdefault((layers[lay], net), []).append(g)

    for node in children(tree, "segment"):
        w = float(value(node, "width"))
        a, b = _xf(*_xy(node, "start"), frame), _xf(*_xy(node, "end"), frame)
        add(
            value(node, "layer"),
            value(node, "net"),
            geometry.LineString([a, b]).buffer(w / 2, quad_segs=32),
        )
    for node in children(tree, "arc"):
        w = float(value(node, "width"))
        a, m, b = (_xf(*_xy(node, k), frame) for k in ("start", "mid", "end"))
        path = model.arc_points(a, m, b, chord=arc_chord) + [b]
        add(
            value(node, "layer"),
            value(node, "net"),
            geometry.LineString(path).buffer(w / 2, quad_segs=32),
        )
    for node in children(tree, "zone"):
        net = value(node, "net_name") or value(node, "net")
        for fp in children(node, "filled_polygon"):
            lay = value(fp, "layer")
            pts = _pts(fp, frame)
            if len(pts) >= 3:
                add(lay, net, geometry.Polygon(pts).buffer(0))
    vias = []
    for node in children(tree, "via"):
        at = _xf(*_xy(node, "at"), frame)
        lays = child(node, "layers")
        span = [str(v) for v in lays[1:]] if lays else []
        net = value(node, "net")
        size = float(value(node, "size"))
        vias.append(
            dict(at=list(at), drill=float(value(node, "drill")), size=size, net=net, layers=span)
        )
        # the via's pad is copper of its own (a zone fill does not include it): a through via
        # (F.Cu .. B.Cu) has a pad on every mapped copper layer, a blind one on its two ends
        through = set(span) == {"F.Cu", "B.Cu"}
        for lay in layers:
            if through or lay in span:
                add(lay, net, geometry.Point(*at).buffer(size / 2, quad_segs=32))
    skipped: List[str] = []
    if pads:
        pad_shapes, holes, skipped = read_kicad_pads(tree, layers, frame, keep, geometry)
        for lay, net, g in pad_shapes:
            shapes.setdefault((layers[lay], net), []).append(g)
        vias += holes
    merged = {k: ops.unary_union(v) for k, v in shapes.items()}
    return dict(copper=merged, vias=vias, pads_skipped=skipped)


def from_kicad(
    path: str,
    box: Sequence[float],
    layers: Dict[str, str],
    stack: Tuple[List[Dict[str, Any]], List[Dict[str, Any]]],
    frame=(0.0, 0.0, True),
    nets: Optional[Iterable[str]] = None,
    via_span: Tuple[str, str] = ("zmin", "L1"),
    air: float = 1.3,
    floor: str = "metal",
    refit: bool = True,
    floor_metal: Optional[Dict[str, Any]] = None,
    pads: bool = True,
) -> Dict[str, Any]:
    """The copper of a zone-filled KiCad board inside ``box`` [x0, x1, y0, y1] as a planar
    document over ``stack`` (dielectrics, layers). The floor (z = 0) is a ``metal`` wall
    (``floor_metal``, default the radar60 feed stack's L2) or ``pec``. Ports are left to the
    caller."""
    geometry, _ = clean._shapely()
    with open(path, encoding="utf-8") as fh:
        raw = read_kicad_copper(fh.read(), layers, frame, nets, pads=pads)
    diel, lays = stack
    z_top = max(
        [d["z1"] for d in diel]
        + [la["z"] + (la.get("t", 0.0) if la["model"] == "solid" else 0.0) for la in lays]
    )
    doc = model.new(
        os.path.basename(path), [*map(float, box), 0.0, round(z_top + air, 6)], _floor(floor)
    )
    _floor_metal(doc, floor, floor_metal)
    doc["stack"] = dict(dielectrics=diel, layers=lays)
    crop = geometry.box(box[0], box[2], box[1], box[3])
    moved = {}
    for (lay, net), g in sorted(raw["copper"].items()):
        g = g.intersection(crop)
        polys = clean.clean_geometry(g, refit=refit)
        if polys:
            doc["conductors"].append(dict(layer=lay, net=net, polygons=polys))
            moved[f"{lay}/{net}"] = round(clean.cleaning_change(g, polys), 6)
    for v in raw["vias"]:
        x, y = v["at"]
        if box[0] < x < box[1] and box[2] < y < box[3] and (nets is None or v["net"] in set(nets)):
            doc["vias"].append(
                dict(
                    at=[x, y],
                    drill=v["drill"],
                    **{"from": via_span[0], "to": via_span[1]},
                    net=v["net"],
                )
            )
    doc["provenance"] = dict(
        source=os.path.basename(path),
        generator="yapnr.rf.planar.adapters.from_kicad",
        frame=list(frame),
        layers=layers,
        cleaning_xor_mm2=moved,
        pads_skipped=raw.get("pads_skipped", []),
    )
    return model.check(doc)
