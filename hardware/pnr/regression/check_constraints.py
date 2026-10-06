"""Independent placement-constraint checker for any tool's routed KiCad board.

Reads the saved ``.kicad_pcb`` with KiCad's own Python (pcbnew), never the engine's
outputs, and verifies a rung's tool-neutral ``checks`` list (hard_rungs.py), one
verdict per check with the measured value:

    satisfied | violated | error

Frame: millimetres from the board outline's lower-left corner, y up (the ladder's
engine frame); a part's position is its footprint origin and its rotation KiCad's
footprint orientation. Courtyards are the footprint's own courtyard layer (the
bottom courtyard for a flipped part), as an axis-aligned box. A part's side is its
footprint layer, confirmed by its surface pads (a part marked flipped whose pads stay
on F.Cu is on neither side).

Check kinds: ``inside_board``, ``side``, ``fixed``, ``edge``, ``orientation``,
``keepout``, ``region``, ``proximity``, ``line``, ``pair_bridge`` (a diff pair's two
series parts, such as a USB board's resistors, stay geometrically coupled across
them and, with ``max_pitch_mm``, side by side), ``align``, ``plane``,
``microvia_span``, ``copper_digest``, ``no_copper``,
and for area-array parts
``via_class`` (the size and site of a part's plane vias), ``escape`` (each listed ball's
copper reaches a via or leaves the courtyard) and ``pad_distance`` (parts' pads near their
net's pads on an anchor); for supplies ``unconnected`` (exactly the listed pads are cut off
from the rest of their net), ``rail_zones`` (each rail of ``nets`` fills as one piece; a
candidate the engine decided to trace, named in ``trace_nets``, fills none) and ``ir_drop``
(a rail's DC drop within its budget, measured by pnr.ir_extract).

    python3 check_constraints.py BOARD.kicad_pcb --spec SPEC.json --out OUT.json

``SPEC.json`` is a ladder ``design.json`` or a benchmark ``spec.json`` (either holds
``checks``; a design without them gets ``hard_rungs.derived_checks``). Exit status 0 when
every check is satisfied, 1 when one is violated or could not be evaluated.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import pcbnew

TOL = 1e-3  # mm


def mm(v):
    return pcbnew.ToMM(v)


class Board:
    def __init__(self, path):
        self.path = str(path)
        self.board = pcbnew.LoadBoard(str(path))
        xs, ys = [], []
        for d in self.board.GetDrawings():
            if d.GetLayer() == pcbnew.Edge_Cuts:
                box = d.GetBoundingBox()
                xs += [box.GetLeft(), box.GetRight()]
                ys += [box.GetTop(), box.GetBottom()]
        if not xs:
            raise ValueError("board has no Edge.Cuts outline")
        # Edge.Cuts strokes have width: the outline is the stroke centre line.
        width = max(
            (d.GetWidth() for d in self.board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts),
            default=0,
        )
        self.x0, self.x1 = mm(min(xs) + width / 2), mm(max(xs) - width / 2)
        self.y0, self.y1 = mm(min(ys) + width / 2), mm(max(ys) - width / 2)  # KiCad y-down
        self.w, self.h = self.x1 - self.x0, self.y1 - self.y0
        self.fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()}

    def pos(self, ref):
        p = self.fps[ref].GetPosition()
        return (mm(p.x) - self.x0, self.y1 - mm(p.y))

    def pad_pos(self, ref, pad):
        """The centre of ``ref``'s first pad numbered ``pad``, in the checker's frame."""
        hit = self.fps[ref].FindPadByNumber(str(pad))
        if hit is None:
            raise ValueError("%s has no pad %r" % (ref, pad))
        p = hit.GetPosition()
        return (mm(p.x) - self.x0, self.y1 - mm(p.y))

    def rot(self, ref):
        return self.fps[ref].GetOrientationDegrees() % 360

    def side(self, ref):
        """``top`` or ``bottom`` by the footprint's layer (KiCad Flip), or ``mixed``
        when its surface pads (one copper layer) contradict it: a footprint marked
        flipped whose surface pads all stay on F.Cu (or the reverse) was never
        mirrored, and is on neither side. Drilled pads span both and do not count."""
        fp = self.fps[ref]
        side, copper = ("bottom", pcbnew.B_Cu) if fp.IsFlipped() else ("top", pcbnew.F_Cu)
        surface = [
            pad.GetLayerSet()
            for pad in fp.Pads()
            if pad.GetLayerSet().Contains(pcbnew.F_Cu) != pad.GetLayerSet().Contains(pcbnew.B_Cu)
        ]
        if surface and not any(layers.Contains(copper) for layers in surface):
            return "mixed"
        return side

    def courtyard(self, ref):
        """(x0, y0, x1, y1) in the engine frame."""
        fp = self.fps[ref]
        layer = pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd
        box = None
        try:
            poly = fp.GetCourtyard(layer)
            if poly.OutlineCount():
                box = poly.BBox()
        except Exception:  # pragma: no cover - version shim
            box = None
        if box is None:
            box = fp.GetBoundingBox(False)
        left, right = mm(box.GetLeft()) - self.x0, mm(box.GetRight()) - self.x0
        top, bottom = self.y1 - mm(box.GetTop()), self.y1 - mm(box.GetBottom())
        return (left, bottom, right, top)

    def refs(self, sel):
        return sorted(self.fps) if sel in (None, "*") else list(sel)


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > TOL and h > TOL else 0.0


def check_inside_board(b, c):
    worst, out = 0.0, {}
    for ref in b.refs(c.get("refs")):
        x0, y0, x1, y1 = b.courtyard(ref)
        beyond = max(-x0, -y0, x1 - b.w, y1 - b.h, 0.0)
        if beyond > TOL:
            out[ref] = round(beyond, 3)
        worst = max(worst, beyond)
    return not out, dict(max_outside_mm=round(worst, 3), outside=out), dict(max_outside_mm=0)


def check_side(b, c):
    wrong = [r for r in b.refs(c.get("refs")) if b.side(r) != c["side"]]
    return not wrong, dict(wrong_side=wrong), dict(side=c["side"])


def check_fixed(b, c):
    ref = c["ref"]
    x, y = b.pos(ref)
    dist = math.dist((x, y), c["at"])
    rot = b.rot(ref)
    ok = (
        dist <= c.get("tol_mm", 0.01) + 1e-6
        and angle_diff(rot, c.get("rot", 0)) <= 0.01
        and b.side(ref) == c.get("side", "top")
    )
    return (
        ok,
        dict(at=[round(x, 4), round(y, 4)], offset_mm=round(dist, 4), rot=rot, side=b.side(ref)),
        dict(at=c["at"], rot=c.get("rot", 0), side=c.get("side", "top"), tol_mm=c.get("tol_mm")),
    )


def edge_distance(b, ref, edge):
    x0, y0, x1, y1 = b.courtyard(ref)
    return dict(south=y0, north=b.h - y1, west=x0, east=b.w - x1)[edge]


def check_edge(b, c):
    d = edge_distance(b, c["ref"], c["edge"])
    return (
        -TOL <= d <= c["max_mm"] + TOL,
        dict(distance_mm=round(d, 3)),
        dict(edge=c["edge"], max_mm=c["max_mm"]),
    )


def check_orientation(b, c):
    rot = b.rot(c["ref"])
    return angle_diff(rot, c["rot"]) <= 0.01, dict(rot=rot), dict(rot=c["rot"])


def check_keepout(b, c):
    hits = {}
    for ref in b.refs(c.get("refs")):
        area = overlap(b.courtyard(ref), c["rect"])
        if area:
            hits[ref] = round(area, 3)
    return not hits, dict(intruding_mm2=hits), dict(rect=c["rect"])


def check_region(b, c):
    x0, y0, x1, y1 = c["rect"]
    out = {}
    for ref in b.refs(c["refs"]):
        a0, b0, a1, b1 = b.courtyard(ref)
        beyond = max(x0 - a0, y0 - b0, a1 - x1, b1 - y1, 0.0)
        if beyond > TOL:
            out[ref] = round(beyond, 3)
    return not out, dict(outside_mm=out), dict(rect=c["rect"])


def check_proximity(b, c):
    """Each ref's position within ``max_mm`` of the anchor's position, or, with
    ``anchor_pad``, of the centre of that pad of the anchor (a pad-anchored hard group)."""
    pad = c.get("anchor_pad")
    anchor = b.pos(c["anchor"]) if pad is None else b.pad_pos(c["anchor"], pad)
    dists = {r: round(math.dist(anchor, b.pos(r)), 3) for r in c["refs"]}
    worst = max(dists.values())
    expected = dict(max_mm=c["max_mm"])
    if pad is not None:
        expected["anchor_pad"] = str(pad)
    return worst <= c["max_mm"] + TOL, dict(distance_mm=dists), expected


def check_line(b, c):
    """Ordered members on one cardinal line, consecutive origins ``pitch_mm`` apart
    in member order, each turned ``rot`` relative to the line, all on one side."""
    refs = c["refs"]
    pts = [b.pos(r) for r in refs]
    dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
    turn = round(math.degrees(math.atan2(dy, dx)) / 90.0) * 90 % 360
    ux, uy = round(math.cos(math.radians(turn))), round(math.sin(math.radians(turn)))
    want_rot = (c.get("rot", 0) + turn) % 360
    step_err = 0.0
    for a, z in zip(pts, pts[1:]):
        want = (a[0] + ux * c["pitch_mm"], a[1] + uy * c["pitch_mm"])
        step_err = max(step_err, math.dist(want, z))
    rot_err = max(angle_diff(b.rot(r), want_rot) for r in refs)
    sides = sorted({b.side(r) for r in refs})
    ok = step_err <= c.get("tol_mm", 0.01) + 1e-6 and rot_err <= 0.01 and len(sides) == 1
    return (
        ok,
        dict(
            direction_deg=turn,
            max_step_error_mm=round(step_err, 4),
            max_rot_error_deg=rot_err,
            sides=sides,
        ),
        dict(pitch_mm=c["pitch_mm"], rot=c.get("rot", 0), tol_mm=c.get("tol_mm", 0.01)),
    )


def check_pair_bridge(b, c):
    """Two two-terminal parts in series on a diff pair's legs (a ``line_group``'s
    series resistors, say) stay geometrically coupled: the vector between their
    pair-side pads equals the vector between their far-side pads, on one side of the
    board, so a constant-width corridor could run straight through both -- the
    geometric precondition for ANY router to keep the pair's copper coupled across
    them, measured here from pad positions alone, not from how a particular tool
    routed it. Parallel legs alone do not make the pair *adjacent* (two parts the
    same distance apart anywhere on the board pass just as well); ``max_pitch_mm``,
    where given, also bounds the near-pad-to-near-pad distance, the "side by side"
    half of the owner's review (2026-10-06) that parallelism alone does not catch."""
    ref_a, ref_z = c["refs"]
    near = (b.pad_pos(ref_a, c["near_pad"]), b.pad_pos(ref_z, c["near_pad"]))
    far = (b.pad_pos(ref_a, c["far_pad"]), b.pad_pos(ref_z, c["far_pad"]))
    near_vec = (near[1][0] - near[0][0], near[1][1] - near[0][1])
    far_vec = (far[1][0] - far[0][0], far[1][1] - far[0][1])
    err = math.dist(near_vec, far_vec)
    pitch = math.dist(*near)
    same_side = b.side(ref_a) == b.side(ref_z)
    max_pitch = c.get("max_pitch_mm")
    ok = (
        err <= c.get("tol_mm", 0.05) + 1e-6
        and same_side
        and (max_pitch is None or pitch <= max_pitch + 1e-6)
    )
    limit = dict(tol_mm=c.get("tol_mm", 0.05))
    if max_pitch is not None:
        limit["max_pitch_mm"] = max_pitch
    return (
        ok,
        dict(
            near_vec_mm=[round(v, 4) for v in near_vec],
            far_vec_mm=[round(v, 4) for v in far_vec],
            mismatch_mm=round(err, 4),
            pitch_mm=round(pitch, 4),
            same_side=same_side,
        ),
        limit,
    )


def check_align(b, c):
    k = 1 if c["axis"] == "y" else 0
    values = [b.pos(r)[k] for r in c["refs"]]
    spread = max(values) - min(values)
    return spread <= c["tol_mm"] + 1e-6, dict(spread_mm=round(spread, 4)), dict(tol_mm=c["tol_mm"])


def check_plane(b, c):
    """The layer is a plane of ``net``: its zones fill at least ``min_fill_fraction``
    of the outline, and no other copper pour shares the layer. (The judge's rules
    keep tracks off a plane layer; a zone of another net, or of no net, there would
    carry that net, or nothing, on the plane instead.)"""
    board = b.board
    lid = board.GetLayerID(c["layer"])
    on_layer = [z for z in board.Zones() if not z.GetIsRuleArea() and z.IsOnLayer(lid)]
    zones = [z for z in on_layer if z.GetNetname() == c["net"]]
    foreign = [z for z in on_layer if z.GetNetname() != c["net"]]
    limit = dict(min_fill_fraction=c["min_fill_fraction"], foreign_fill_fraction=0.0)
    if not zones:
        return (False, dict(zones=0, fill_fraction=0.0, foreign_zones=len(foreign)), limit)
    filled_here = False
    if not all(z.IsFilled() for z in on_layer):
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())  # in memory only; never saved
        filled_here = True
    area = sum(mm(mm(z.GetFilledPolysList(lid).Area())) for z in zones)
    fraction = area / (b.w * b.h)
    foreign_area = sum(mm(mm(z.GetFilledPolysList(lid).Area())) for z in foreign)
    foreign_fraction = foreign_area / (b.w * b.h)
    return (
        fraction + 1e-9 >= c["min_fill_fraction"] and foreign_area <= 1e-6,
        dict(
            zones=len(zones),
            fill_fraction=round(fraction, 4),
            filled_by_checker=filled_here,
            foreign_zones=sorted({z.GetNetname() or "<no net>" for z in foreign}),
            foreign_fill_fraction=round(foreign_fraction, 4),
        ),
        limit,
    )


def check_microvia_span(b, c):
    """Every microvia joins neighbouring copper layers: it crosses at most
    ``max_dielectrics`` dielectric layers (one, a laser drill's depth). KiCad's DRC
    accepts a microvia of any span, so without this a board passes the judge with
    "microvias" that no laser drill makes. Spans count in the board's copper order."""
    board = b.board
    order = {lid: i for i, lid in enumerate(board.GetEnabledLayers().CuStack())}
    limit = c.get("max_dielectrics", 1)
    count, deep = 0, []
    for via in board.GetTracks():
        if via.Type() != pcbnew.PCB_VIA_T or via.GetViaType() != pcbnew.VIATYPE_MICROVIA:
            continue
        count += 1
        top, bottom = via.TopLayer(), via.BottomLayer()
        if abs(order[bottom] - order[top]) > limit:
            p = via.GetPosition()
            deep.append(
                dict(
                    net=via.GetNetname(),
                    layers=[board.GetLayerName(top), board.GetLayerName(bottom)],
                    at=[round(mm(p.x) - b.x0, 3), round(b.y1 - mm(p.y), 3)],
                )
            )
    return (
        not deep,
        dict(microvias=count, too_deep=len(deep), examples=deep[:5]),
        dict(max_dielectrics=limit),
    )


def group_of(item):
    """Name of the outermost KiCad group holding ``item`` (None outside any)."""
    group = item.GetParentGroup()
    name = None
    while group is not None:
        name = group.GetName()
        owner = group.AsEdaItem() if hasattr(group, "AsEdaItem") else group
        group = owner.GetParentGroup()
    return name


VIA_KINDS = {
    int(pcbnew.VIATYPE_THROUGH): "through",
    int(pcbnew.VIATYPE_BLIND): "blind",
    int(pcbnew.VIATYPE_BURIED): "buried",
    int(pcbnew.VIATYPE_MICROVIA): "micro",
}


def check_copper_digest(b, c):
    """The copper of KiCad group ``group`` is the copper the rung gave: sha256 over
    every track, arc and via of the group, to the nanometre, in the frame of footprint
    ``anchor`` (position and orientation) or, without one, of the outline's lower-left
    corner (KiCad's y down): kind, points, width or via size, drill, type and layers,
    and net. (The same rows as the engine's pnr.fixed_copper.block_digest, computed
    here independently.)"""
    import hashlib

    board = b.board
    if c.get("anchor"):
        fp = b.fps[c["anchor"]]
        ox, oy, turn = fp.GetPosition().x, fp.GetPosition().y, fp.GetOrientationDegrees()
    else:
        ox, oy, turn = pcbnew.FromMM(b.x0), pcbnew.FromMM(b.y1), 0.0
    quarter = round(turn / 90.0)
    if abs(turn - 90.0 * quarter) > 1e-9:
        raise ValueError("copper_digest: anchor turned off a quarter turn")

    def local(p):
        dx, dy = p.x - ox, p.y - oy
        for _ in range(quarter % 4):
            dx, dy = -dy, dx
        return "%d %d" % (dx, dy)

    rows = []
    for t in board.GetTracks():
        if group_of(t) != c["group"]:
            continue
        kind, name = t.GetClass(), board.GetLayerName
        if kind == "PCB_VIA":
            row = [
                "via",
                local(t.GetPosition()),
                "d %d" % t.GetWidth(t.TopLayer()),
                "drill %d" % t.GetDrillValue(),
                "type %s" % VIA_KINDS.get(int(t.GetViaType()), str(int(t.GetViaType()))),
                "%s-%s" % (name(t.TopLayer()), name(t.BottomLayer())),
            ]
        elif kind == "PCB_ARC":
            row = ["arc", local(t.GetStart()), local(t.GetMid()), local(t.GetEnd())]
            row += ["w %d" % t.GetWidth(), name(t.GetLayer())]
        else:
            row = ["segment", local(t.GetStart()), local(t.GetEnd())]
            row += ["w %d" % t.GetWidth(), name(t.GetLayer())]
        rows.append("|".join(row + [t.GetNetname()]))
    rows.sort()
    digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return (
        digest == c["sha256"],
        dict(sha256=digest, items=len(rows)),
        dict(sha256=c["sha256"]),
    )


def check_no_copper(b, c):
    """No foreign copper in a polygon (board frame, mm) on ``layers``: tracks, arcs
    and vias (``items`` tracks / vias), footprint pads (``pads``) and, when listed,
    the filled copper of zones (``zones``) touching it, unless their net is in
    ``allow_nets`` or they belong to a KiCad group in ``exempt_groups`` (a pad: its
    footprint's group). Zones are judged only when ``items`` lists them."""
    board = b.board
    keep = pcbnew.SHAPE_POLY_SET()
    keep.NewOutline()
    for x, y in c["polygon"]:
        keep.Append(pcbnew.FromMM(b.x0 + x), pcbnew.FromMM(b.y1 - y))
    allowed = set(c.get("allow_nets") or [])
    exempt = set(c.get("exempt_groups") or [])
    items = set(c.get("items") or ("tracks", "vias", "pads"))
    candidates = []
    for t in board.GetTracks():
        is_via = t.GetClass() == "PCB_VIA"
        if ("vias" if is_via else "tracks") in items:
            candidates.append((t, "via" if is_via else "track", group_of(t)))
    if "pads" in items:
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                candidates.append((pad, "pad", group_of(fp)))
    if "zones" in items:
        for zone in board.Zones():
            if not zone.GetIsRuleArea():
                candidates.append((zone, "zone", group_of(zone)))
    hits = []
    for layer_name in c["layers"]:
        layer = board.GetLayerID(layer_name)
        for item, kind, group in candidates:
            if item.GetNetname() in allowed or (group and group in exempt):
                continue
            if not item.IsOnLayer(layer):
                continue
            if kind == "zone":
                shape = pcbnew.SHAPE_POLY_SET(item.GetFilledPolysList(layer))
            else:
                shape = pcbnew.SHAPE_POLY_SET()
                item.TransformShapeToPolygon(shape, layer, 0, 1000, pcbnew.ERROR_INSIDE)
            shape.BooleanIntersection(keep)
            if shape.OutlineCount() and shape.Area() > 0:
                p = item.GetPosition()
                hits.append(
                    dict(
                        kind=kind,
                        net=item.GetNetname(),
                        layer=layer_name,
                        at=[round(mm(p.x) - b.x0, 3), round(b.y1 - mm(p.y), 3)],
                    )
                )
    return (
        not hits,
        dict(foreign=len(hits), examples=hits[:5]),
        dict(layers=c["layers"], allow_nets=sorted(allowed), exempt_groups=sorted(exempt)),
    )


def _ref_pads(b, ref):
    """{pad name: (engine-frame centre, pad)} of a part's pads."""
    out = {}
    for pad in b.fps[ref].Pads():
        p = pad.GetPosition()
        out[pad.GetNumber()] = ((mm(p.x) - b.x0, b.y1 - mm(p.y)), pad)
    return out


def check_via_class(b, c):
    """Every via of ``nets`` inside ``ref``'s courtyard is ``diameter_mm``/``drill_mm``
    and, with ``site: interstitial``, sits at the centre of a cell of the part's ball
    lattice (pitch per axis from its pads, in the footprint's own frame)."""
    x0, y0, x1, y1 = b.courtyard(c["ref"])
    nets = set(c["nets"])
    fp = b.fps[c["ref"]]
    # The ball lattice in the footprint's own frame: pitch per axis, origin at a ball.
    local = [(mm(p.GetFPRelativePosition().x), mm(p.GetFPRelativePosition().y)) for p in fp.Pads()]

    def pitch(values):
        v = sorted({round(x, 4) for x in values})
        gaps = [round(q - p, 3) for p, q in zip(v, v[1:]) if q - p > TOL]
        return min(gaps, key=lambda g: (-gaps.count(g), g)) if gaps else 0.0

    px, py = pitch([p[0] for p in local]), pitch([p[1] for p in local])
    ox, oy = local[0] if local else (0.0, 0.0)
    angle = math.radians(fp.GetOrientationDegrees())
    centre = fp.GetPosition()

    def interstitial(via):
        # The via in the footprint frame (KiCad y down, as GetFPRelativePosition).
        dx, dy = mm(via.x - centre.x), mm(via.y - centre.y)
        u = dx * math.cos(angle) - dy * math.sin(angle)
        v = dx * math.sin(angle) + dy * math.cos(angle)
        if fp.IsFlipped():
            u = -u
        fu, fv = (u - ox) / px - 0.5, (v - oy) / py - 0.5
        return abs(fu - round(fu)) * px <= 0.005 and abs(fv - round(fv)) * py <= 0.005

    wrong, off_site, count = [], [], 0
    for t in b.board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetname() not in nets:
            continue
        p = t.GetPosition()
        at = (mm(p.x) - b.x0, b.y1 - mm(p.y))
        if not (x0 <= at[0] <= x1 and y0 <= at[1] <= y1):
            continue
        count += 1
        size = (round(mm(t.GetWidth(pcbnew.F_Cu)), 4), round(mm(t.GetDrillValue()), 4))
        if abs(size[0] - c["diameter_mm"]) > TOL or abs(size[1] - c["drill_mm"]) > TOL:
            wrong.append([round(at[0], 3), round(at[1], 3), *size])
        if c.get("site") == "interstitial" and not (px and py and interstitial(p)):
            off_site.append([round(at[0], 3), round(at[1], 3)])
    return (
        count > 0 and not wrong and not off_site,
        dict(vias=count, wrong_size=wrong[:10], off_site=off_site[:10]),
        dict(diameter_mm=c["diameter_mm"], drill_mm=c["drill_mm"], site=c.get("site")),
    )


def check_escape(b, c):
    """Each listed pad of ``ref`` reaches, through its own net's tracks, a via or a
    point outside the part's courtyard (its copper leaves the ball field)."""
    x0, y0, x1, y1 = b.courtyard(c["ref"])
    pads = _ref_pads(b, c["ref"])
    by_net = {}
    for t in b.board.GetTracks():
        by_net.setdefault(t.GetNetname(), []).append(t)

    def xy(v):
        return (mm(v.x) - b.x0, b.y1 - mm(v.y))

    def outside(p):
        return not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1)

    stuck = []
    for name in c["pads"]:
        centre, pad = pads[name]
        items = by_net.get(pad.GetNetname(), []) if pad.GetNetname() else []
        frontier, seen, ok = [centre], set(), False
        while frontier and not ok:
            p = frontier.pop()
            for i, t in enumerate(items):
                if i in seen:
                    continue
                if t.GetClass() == "PCB_VIA":
                    if math.dist(xy(t.GetPosition()), p) <= mm(t.GetWidth(pcbnew.F_Cu)) / 2 + TOL:
                        ok = True
                        break
                    continue
                a, z = xy(t.GetStart()), xy(t.GetEnd())
                for e, other in ((a, z), (z, a)):
                    if math.dist(e, p) <= TOL:
                        seen.add(i)
                        if outside(other):
                            ok = True
                        frontier.append(other)
                        break
                if ok:
                    break
        if not ok:
            stuck.append(name)
    return (
        not stuck,
        dict(escaped=len(c["pads"]) - len(stuck), pads=len(c["pads"]), not_escaped=stuck),
        dict(pads=len(c["pads"])),
    )


def check_pad_distance(b, c):
    """Each netted pad of ``refs`` is within ``max_mm`` (centre to centre) of a pad of
    ``anchor`` on its own net."""
    anchor = _ref_pads(b, c["anchor"])
    worst, out = 0.0, {}
    for ref in c["refs"]:
        for name, (centre, pad) in sorted(_ref_pads(b, ref).items()):
            net = pad.GetNetname()
            if not net:
                continue
            d = min(
                (math.dist(centre, q) for q, other in anchor.values() if other.GetNetname() == net),
                default=math.inf,
            )
            out["%s.%s" % (ref, name)] = round(d, 3)
            worst = max(worst, d)
    return worst <= c["max_mm"] + TOL, dict(distance_mm=out), dict(max_mm=c["max_mm"])


def check_net_vias(b, c):
    """The vias of ``nets`` number at most ``max`` (default 0: a no-via rule)."""
    nets = set(c["nets"])
    found = []
    for t in b.board.GetTracks():
        if t.GetClass() == "PCB_VIA" and t.GetNetname() in nets:
            p = t.GetPosition()
            found.append([t.GetNetname(), round(mm(p.x) - b.x0, 3), round(b.y1 - mm(p.y), 3)])
    limit = int(c.get("max", 0))
    return len(found) <= limit, dict(vias=len(found), at=found[:10]), dict(max=limit)


def _filled(board):
    """Fill the zones in memory (never saved) when any is not filled."""
    if not all(z.IsFilled() for z in board.Zones() if not z.GetIsRuleArea()):
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        return True
    return False


def check_unconnected(b, c):
    """The pads cut off from the rest of their net (outside its largest connected
    group of pads, zones filled) are exactly ``pads`` ("REF.PAD")."""
    board = b.board
    _filled(board)
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    by_net = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                by_net.setdefault(pad.GetNetCode(), []).append(pad)
    cut = []
    for _code, pads in sorted(by_net.items()):
        if len(pads) < 2:
            continue
        name = {
            id(p): "%s.%s" % (p.GetParentFootprint().GetReference(), p.GetNumber()) for p in pads
        }
        key = {name[id(p)]: p for p in pads}
        groups, seen = [], set()
        for p in pads:
            if name[id(p)] in seen:
                continue
            group = {name[id(p)]}
            for item in conn.GetConnectedItems(p):  # the pad's whole cluster
                if item.GetClass() != "PAD":
                    continue
                q = pcbnew.Cast_to_PAD(item) if hasattr(pcbnew, "Cast_to_PAD") else item
                group.add("%s.%s" % (q.GetParentFootprint().GetReference(), q.GetNumber()))
            group &= set(key)
            seen |= group
            groups.append(group)
        if len(groups) > 1:
            groups.sort(key=lambda g: (-len(g), sorted(g)))
            for g in groups[1:]:
                cut.extend(g)
    want = sorted(c["pads"])
    return sorted(cut) == want, dict(unconnected=sorted(cut)), dict(pads=want)


def check_rail_zones(b, c):
    """Each rail of ``nets`` fills ``layer`` as one piece of at least ``min_area_mm2``;
    a candidate the engine decided to trace (``trace_nets``, a subset of ``nets``)
    fills none there, confirming the engine left it out of the partition. No other
    net (but the ``fill`` net) pours on the layer."""
    board = b.board
    lid = board.GetLayerID(c["layer"])
    _filled(board)
    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.IsOnLayer(lid)]
    trace_nets = set(c.get("trace_nets") or ())
    measured = {}
    ok = True
    for net in c["nets"]:
        pieces, area = 0, 0.0
        for z in zones:
            if z.GetNetname() == net:
                fill = z.GetFilledPolysList(lid)
                pieces += fill.OutlineCount()
                area += mm(mm(fill.Area()))
        measured[net] = dict(pieces=pieces, area_mm2=round(area, 3))
        if net in trace_nets:
            ok = ok and pieces == 0
        else:
            ok = ok and pieces == 1 and area >= c["min_area_mm2"]
    foreign = sorted(
        {z.GetNetname() or "<no net>" for z in zones}
        - set(c["nets"])
        - ({c["fill"]} if c.get("fill") else set())
    )
    measured["foreign"] = foreign
    return (
        ok and not foreign,
        measured,
        dict(pieces=1, min_area_mm2=c["min_area_mm2"], trace_nets=sorted(trace_nets)),
    )


def check_ir_drop(b, c):
    """The rail's DC drop (pnr.ir_extract: filled zones, tracks, vias, pads) from
    ``sources`` to ``sinks`` at ``current_a`` is within ``budget_mv`` (or
    ``budget_mohm``) and reaches every sink."""
    import tempfile

    from pnr.ir_extract import report

    _filled(b.board)
    keys = (
        "net",
        "sources",
        "sinks",
        "exclude",
        "current_a",
        "budget_mv",
        "budget_mohm",
        "temperature_c",
    )
    entry = {k: c[k] for k in keys if c.get(k) is not None}
    entry["two_point"] = False  # the verdict needs the drop, not each sink alone
    with tempfile.TemporaryDirectory() as tmp:
        rep = report(b.board, dict(ir_drop=[entry]), Path(tmp), b.path)[c["net"]]
    measured = dict(
        status=rep["status"],
        opens=rep["opens"],
        worst_drop_mv=rep.get("worst_drop_mv"),
        r_eff_mohm=rep.get("r_eff_mohm"),
    )
    limit = {k: c[k] for k in ("budget_mv", "budget_mohm") if c.get(k) is not None}
    return rep["status"] == "pass", measured, limit


KINDS = dict(
    inside_board=check_inside_board,
    side=check_side,
    fixed=check_fixed,
    edge=check_edge,
    orientation=check_orientation,
    keepout=check_keepout,
    region=check_region,
    proximity=check_proximity,
    line=check_line,
    pair_bridge=check_pair_bridge,
    align=check_align,
    plane=check_plane,
    microvia_span=check_microvia_span,
    copper_digest=check_copper_digest,
    no_copper=check_no_copper,
    via_class=check_via_class,
    escape=check_escape,
    pad_distance=check_pad_distance,
    net_vias=check_net_vias,
    unconnected=check_unconnected,
    rail_zones=check_rail_zones,
    ir_drop=check_ir_drop,
)


def run_checks(board_path, spec):
    b = Board(board_path)
    checks = spec.get("checks")
    if not checks:  # a ladder or showcase design.json: the derived checks
        from hard_rungs import derived_checks

        checks = derived_checks(spec)
    results = []
    for c in checks:
        record = dict(id=c["id"], kind=c["kind"], engine=c.get("engine"))
        try:
            ok, measured, limit = KINDS[c["kind"]](b, c)
            record.update(status="satisfied" if ok else "violated", measured=measured, limit=limit)
        except Exception as error:  # a check that cannot be evaluated is reported, not hidden
            record.update(status="error", error=repr(error))
        results.append(record)
    count = {s: sum(r["status"] == s for r in results) for s in ("satisfied", "violated", "error")}
    return dict(
        schema="ladder-constraint-check-v1",
        board=Path(board_path).name,
        outline_mm=[round(b.w, 4), round(b.h, 4)],
        checks=results,
        summary=dict(
            count,
            total=len(results),
            violated_ids=[r["id"] for r in results if r["status"] != "satisfied"],
            violated_unsupported=[
                r["id"]
                for r in results
                if r["status"] != "satisfied" and r.get("engine") == "unsupported"
            ],
        ),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--exit-zero", action="store_true", help="exit 0 even when a check fails (the runner gates)"
    )
    a = ap.parse_args()
    result = run_checks(a.board, json.loads(a.spec.read_text()))
    a.out.write_text(json.dumps(result, indent=2))
    s = result["summary"]
    print(
        "checks: %d satisfied, %d violated, %d error" % (s["satisfied"], s["violated"], s["error"])
    )
    return 0 if a.exit_zero or s["satisfied"] == s["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
