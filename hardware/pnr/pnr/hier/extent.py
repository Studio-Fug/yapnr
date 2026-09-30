"""Used extent and per-side hull of a routed block layout (N-0001; pure python + numpy).

A library layout is routed on a sub-board (``pnr.hier.native_block``) whose
rectangle is sized from a target utilisation, so its macro used to reserve the
whole rectangle plus the edge clearance. This module measures what the block
really occupies, from the routed ``electrical/board.kicad_pcb`` of the instance
and the member poses of the layout:

* :func:`read_board` - a paren-matched scan of footprints (reference, position,
  layer), tracks, vias, arcs and zones; no KiCad needed;
* :func:`solve_frame` - the board -> block-frame translation from the member
  footprints (every one within :data:`FRAME_TOL_MM`, the tolerance
  ``pnr.hier.assemble`` accepts, and on the member's side);
* :func:`block_geometry` - the used extent (courtyards united with pads and all
  copper grown by the macro margin, and every drilled hole grown by the fab
  hole-to-edge rule) and the per-plane physical shapes: member reservations
  (:func:`pnr.place.geometry.placement_rects`) and pads exactly, outer-layer
  tracks/vias as capsules grown by the block copper clearance, and an ``inner``
  plane (inner-layer tracks, every via barrel, member plated/unplated holes) that
  only drilled parts and other blocks test against;
* :func:`build_hull` - those shapes on the placement lattice of the macro slot
  (pockets closed off by copper filled on the outer sides), stored on
  ``Component.hull``;
* :func:`used_area` - the summed extent area of a library record's instances,
  for ``PNR_LIBRARY_RANK_USED``.

Zones are not measured: ``pnr.hier.assemble`` copies tracks and vias only, and
the full board pours its own planes. Arcs are refused (assemble refuses them too).
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FRAME_TOL_MM = 0.002  # pnr.hier.assemble's default rigid tolerance (2000 nm)
HULL_GRID_MM = 0.25  # the placer's default legalization grid
OUTER = {"F.Cu": "top", "B.Cu": "bottom"}
PLANES = ("top", "bottom", "inner")  # hull planes: outer sides + the union of inner layers


def layer_index(name: str) -> int:
    """Stack position of a copper layer: F.Cu 0, InN.Cu N, B.Cu last (any layer count)."""
    if name == "F.Cu":
        return 0
    if name == "B.Cu":
        return 1 << 16
    m = re.fullmatch(r"In(\d+)\.Cu", name or "")
    if m is None:
        raise ValueError(f"unknown copper layer {name!r}")
    return int(m.group(1))


_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


# ------------------------------------------------------------------ board scan


def _parse(text: str, start: int):
    """Parse one s-expression starting at ``text[start] == '('``; (list, end)."""
    stack: List[list] = []
    for m in _TOKEN.finditer(text, start):
        tok = m.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            if not stack:
                return done, m.end()
            stack[-1].append(done)
        else:
            stack[-1].append(tok[1:-1] if tok[0] == '"' else tok)
    raise ValueError("unbalanced s-expression")


def _top_items(text: str, heads):
    """Top-level children ``(head ...)`` of the ``(kicad_pcb ...)`` root, parsed."""
    depth, i, n = 0, 0, len(text)
    out = []
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while True:
                j = text.index('"', j)
                if text[j - 1] != "\\":
                    break
                j += 1
            i = j + 1
            continue
        if ch == "(":
            if depth == 1:
                m = re.match(r"\(([A-Za-z_]+)", text[i : i + 40])
                if m and m.group(1) in heads:
                    item, end = _parse(text, i)
                    out.append(item)
                    i = end
                    continue
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return out


def _child(item, head):
    for x in item[1:]:
        if isinstance(x, list) and x and x[0] == head:
            return x
    return None


def _children(item, head):
    return [x for x in item[1:] if isinstance(x, list) and x and x[0] == head]


def _walk(item, head):
    for x in item[1:]:
        if isinstance(x, list):
            if x and x[0] == head:
                yield x
            yield from _walk(x, head)


def _footprint(item):
    at = _child(item, "at")
    layer = _child(item, "layer")
    ref = None
    for p in _children(item, "property"):
        if len(p) > 2 and p[1] == "Reference":
            ref = p[2]
    if ref is None:
        for t in _children(item, "fp_text"):
            if len(t) > 2 and t[1] == "reference":
                ref = t[2]
    if at is None or layer is None or ref is None:
        raise ValueError("footprint without reference/at/layer")
    rot = float(at[3]) if len(at) > 3 else 0.0
    return ref, (float(at[1]), float(at[2]), rot, layer[1])


_BOARDS: Dict[tuple, dict] = {}


def read_board(path) -> dict:
    """{footprints: {ref: (x, y, rot, layer)}, segments: [(x0, y0, x1, y1, w, layer)],
    vias: [(x, y, size, (layer_a, layer_b))], via_drills: [drill] (parallel to vias; 0 when
    the via has none), arcs: n, zones: n}, KiCad mm (y down).

    Memoised by (real path, mtime, size)."""
    p = Path(path).resolve()
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _BOARDS.get(key)
    if hit is not None:
        return hit
    text = p.read_text()
    if not text.lstrip().startswith("(kicad_pcb"):
        raise ValueError(f"{p}: not a kicad_pcb file")
    items = _top_items(text, {"footprint", "segment", "via", "arc", "zone"})
    fps, segs, vias, drills, arcs, zones = {}, [], [], [], 0, 0
    for it in items:
        head = it[0]
        if head == "footprint":
            ref, pose = _footprint(it)
            if ref in fps:
                raise ValueError(f"{p}: duplicate footprint {ref}")
            fps[ref] = pose
        elif head == "segment":
            s, e, w, la = (
                _child(it, "start"),
                _child(it, "end"),
                _child(it, "width"),
                _child(it, "layer"),
            )
            segs.append((float(s[1]), float(s[2]), float(e[1]), float(e[2]), float(w[1]), la[1]))
        elif head == "via":
            at = _child(it, "at")
            sizes = [float(x[1]) for x in [_child(it, "size")] + list(_walk(it, "size")) if x]
            layers = _child(it, "layers")
            if not sizes:
                raise ValueError(f"{p}: via without size")
            vias.append(
                (
                    float(at[1]),
                    float(at[2]),
                    max(sizes),
                    tuple(layers[1:3]) if layers else ("F.Cu", "B.Cu"),
                )
            )
            drill = _child(it, "drill")
            drills.append(float(drill[1]) if drill and len(drill) > 1 else 0.0)
        elif head == "arc":
            arcs += 1
        elif head == "zone":
            zones += 1
    out = dict(footprints=fps, segments=segs, vias=vias, via_drills=drills, arcs=arcs, zones=zones)
    if len(_BOARDS) > 64:
        _BOARDS.clear()
    _BOARDS[key] = out
    return out


# ------------------------------------------------------------------ frame


def _median(v):
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def solve_frame(footprints: dict, components) -> Tuple[float, float, float]:
    """(tx, ty, residual): block (x, y) = (x_k - tx, ty - y_k) for board point (x_k, y_k).

    Every member must be on the board, on its side, within FRAME_TOL_MM."""
    comps = list(components)
    missing = sorted(c.ref for c in comps if c.ref not in footprints)
    if missing:
        raise ValueError(f"members missing on the block board: {missing[:6]}")
    extra = sorted(set(footprints) - {c.ref for c in comps})
    if extra:
        raise ValueError(f"block board has footprints outside the layout: {extra[:6]}")
    for c in comps:
        layer = footprints[c.ref][3]
        if (layer == "B.Cu") != (c.side == "bottom"):
            raise ValueError(f"{c.ref}: board layer {layer} vs layout side {c.side}")
    txs = [footprints[c.ref][0] - c.pos[0] for c in comps]
    tys = [footprints[c.ref][1] + c.pos[1] for c in comps]
    tx, ty = _median(txs), _median(tys)
    residual = max(max(abs(v - tx) for v in txs), max(abs(v - ty) for v in tys))
    if residual > FRAME_TOL_MM:
        raise ValueError(
            f"block board is not a rigid copy of the layout: {residual * 1000:.1f} um residual"
        )
    return tx, ty, residual


# ------------------------------------------------------------------ rules


def copper_clearance(rules: dict) -> float:
    """Clearance block copper keeps from anything nested next to it (mm): the
    largest copper-to-copper / hole-to-copper value of the fab profile and the
    net classes (0.35 on jlc-pofv, set by the PTH hole clearance)."""
    fab = rules.get("fab") or {}
    values = [
        float(fab[k])
        for k in (
            "clearance_mm",
            "smd_pad_clearance_mm",
            "hole_clearance_mm",
            "pth_hole_clearance_mm",
            "npth_hole_clearance_mm",
            "via_to_smd_pad_mm",
        )
        if fab.get(k) is not None
    ]
    values += [
        float(nc["clearance_mm"])
        for nc in rules.get("net_classes") or []
        if isinstance(nc, dict) and nc.get("clearance_mm") is not None
    ]
    values.append(float(rules.get("default_clearance_mm") or 0.0))
    return max(values)


def macro_margin(rules: dict) -> float:
    """The margin collapse() reserves around a block (the fab edge clearance)."""
    return float(rules.get("fab", {}).get("edge_clearance_mm", 0.2))


def track_width(rules: dict) -> float:
    return float((rules.get("fab") or {}).get("track_width_mm") or 0.2)


def hole_edge(rules: dict) -> Optional[float]:
    """The fab hole-to-edge rule (drill wall to the outline centreline), None when unset."""
    v = (rules.get("fab") or {}).get("hole_to_edge_mm")
    return None if v is None else float(v)


# ------------------------------------------------------------------ geometry


@dataclass
class BlockGeometry:
    ok: bool
    reason: str = ""
    extent: Optional[Tuple[float, float, float, float]] = None  # block frame (x0, y0, x1, y1)
    shapes: Dict[str, Dict[str, list]] = field(
        default_factory=dict
    )  # plane -> rect / cap (block frame)
    c_cu: float = 0.0
    margin: float = 0.0
    track_width: float = 0.2
    residual_mm: float = 0.0
    source: str = ""
    stats: dict = field(default_factory=dict)

    @property
    def size(self):
        x0, y0, x1, y1 = self.extent
        return (x1 - x0, y1 - y0)

    @property
    def area(self):
        w, h = self.size
        return w * h


def block_geometry(
    components, board_path, rules: dict, margin: Optional[float] = None
) -> BlockGeometry:
    """Used extent and per-plane shapes of a routed block, in the frame of ``components``.

    ``components`` are the members posed as in the routed board (``instance_board``
    of the layout, or the instance's ``evaluated-placed.json``). Raises on a board
    that does not match them or that contains arcs.

    The extent holds every member courtyard, every pad and track/via copper grown
    by the margin (the fab edge clearance) and every drilled hole (via drills,
    member PTH/NPTH drills) grown by the fab hole-to-edge rule, so a shrunk macro
    kept inside the outline keeps both rules. The ``inner`` plane collects what a
    drilled part (or another block's inner copper) must clear: inner-layer tracks,
    every via barrel and the member drilled pads."""
    from pnr.place.geometry import courtyard_rect, pad_rects, placement_rects

    comps = list(components)
    board = read_board(board_path)
    if board["arcs"]:
        raise ValueError(f"block board has {board['arcs']} arcs (pnr.hier.assemble refuses arcs)")
    tx, ty, residual = solve_frame(board["footprints"], comps)
    m = macro_margin(rules) if margin is None else float(margin)
    c_cu = copper_clearance(rules)
    h_edge = hole_edge(rules)
    shapes = {s: dict(rect=[], cap=[]) for s in PLANES}
    xs0, ys0, xs1, ys1 = [], [], [], []

    def grow_box(x0, y0, x1, y1, e):
        xs0.append(x0 - e)
        ys0.append(y0 - e)
        xs1.append(x1 + e)
        ys1.append(y1 + e)

    n_holes = 0
    for c in comps:
        cr = courtyard_rect(c)
        grow_box(cr.left, cr.bottom, cr.right, cr.top, 0.0)
        for side, r in placement_rects(c):
            if side in OUTER.values():
                shapes[side]["rect"].append([r.left, r.bottom, r.right, r.top])
        swap = int(round(c.rot)) % 180 == 90
        for pad, (_, _, r) in zip(c.pads, pad_rects(c)):
            if pad.through_hole and h_edge is not None and min(pad.drill_size) > 0:
                dw, dh = (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
                grow_box(r.cx - dw / 2, r.cy - dh / 2, r.cx + dw / 2, r.cy + dh / 2, h_edge)
                n_holes += 1
            if pad.through_hole:
                # pad or drill extent, whichever is larger (an NPTH may carry no pad size)
                dw, dh = (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
                w, h = max(r.w, dw), max(r.h, dh)
                if w > 0 and h > 0:
                    for side in PLANES:
                        shapes[side]["rect"].append(
                            [r.cx - w / 2, r.cy - h / 2, r.cx + w / 2, r.cy + h / 2]
                        )
            if r.w <= 0 or r.h <= 0:
                continue
            grow_box(r.left, r.bottom, r.right, r.top, m)
            if not pad.through_hole:
                shapes[c.side]["rect"].append([r.left, r.bottom, r.right, r.top])
    n_inner = 0
    for x0, y0, x1, y1, w, layer in board["segments"]:
        a = (x0 - tx, ty - y0)
        b = (x1 - tx, ty - y1)
        grow_box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]), w / 2 + m)
        side = OUTER.get(layer)
        if side is None:
            layer_index(layer)  # an unknown layer name is refused, not guessed
            n_inner += 1
            side = "inner"
        shapes[side]["cap"].append([a[0], a[1], b[0], b[1], w / 2 + c_cu])
    drills = board.get("via_drills") or [0.0] * len(board["vias"])
    for (x, y, size, (la, lb)), drill in zip(board["vias"], drills):
        p = (x - tx, ty - y)
        grow_box(p[0], p[1], p[0], p[1], size / 2 + m)
        if h_edge is not None and drill > 0:
            grow_box(p[0], p[1], p[0], p[1], drill / 2 + h_edge)
        span = sorted(layer_index(v) for v in (la, lb))
        for layer, side in OUTER.items():
            if span[0] <= layer_index(layer) <= span[1]:
                shapes[side]["cap"].append([p[0], p[1], p[0], p[1], size / 2 + c_cu])
        # every via reaches an inner layer (blind/buried ones only inner ones)
        shapes["inner"]["cap"].append([p[0], p[1], p[0], p[1], size / 2 + c_cu])
    extent = (min(xs0), min(ys0), max(xs1), max(ys1))
    stats = dict(
        segments=len(board["segments"]),
        vias=len(board["vias"]),
        inner_segments=n_inner,
        zones_ignored=board["zones"],
        footprints=len(comps),
        holes=n_holes,
        via_drills=sum(1 for d in drills if d > 0),
    )
    return BlockGeometry(
        ok=True,
        extent=extent,
        shapes=shapes,
        c_cu=c_cu,
        margin=m,
        track_width=track_width(rules),
        residual_mm=residual,
        source=str(board_path),
        stats=stats,
    )


def safe_geometry(components, board_path, rules, margin=None) -> BlockGeometry:
    """:func:`block_geometry`, or ``ok=False`` with the reason (the macro then keeps its rectangle)."""
    if not board_path:
        return BlockGeometry(ok=False, reason="no routed block board (instance has no native dir)")
    if not Path(board_path).exists():
        return BlockGeometry(ok=False, reason=f"routed block board missing: {board_path}")
    try:
        return block_geometry(components, board_path, rules, margin)
    except Exception as error:  # a layout we cannot measure keeps its full rectangle
        return BlockGeometry(
            ok=False, reason=f"{type(error).__name__}: {error}", source=str(board_path)
        )


# ------------------------------------------------------------------ hull


def build_hull(
    geo: BlockGeometry, origin, courtyard, clearance: float, grid_mm: float = HULL_GRID_MM
) -> dict:
    """``Component.hull`` of a macro with frame ``origin`` (block frame) and ``courtyard`` (w, h).

    The lattice is that of the default legalizer slot, ceil((w + clearance)/g)
    cells per axis and centred on the macro centre, so the cover cells coincide
    with the board grid wherever the legalizer puts the macro, at every quarter
    turn. Free pockets that cannot be left on their side are filled (outer sides
    only: the ``inner`` plane is a keep-out for drilled parts and other blocks'
    inner copper, not a routing side).

    Hull shapes act like courtyards: the legalizer keeps every other courtyard
    ``clearance`` away from them (and the overlap check keeps it off them). Member
    courtyards and pads enter as they are; copper capsules enter with radius
    w/2 + c_cu - min(clearance, c_cu), so that spacing leaves exactly the block
    copper clearance ``c_cu`` between block copper and a nested courtyard (which
    holds the nested part's copper).

    The ``inner`` plane (inner-layer tracks, via barrels, member drilled pads)
    uses the same relief: a drilled part's courtyard, which holds its holes,
    ends up at least ``c_cu`` (the PTH hole-to-copper rule on jlc-pofv) from
    block copper on every inner layer.

    Raises ValueError when a relieved copper capsule reaches past the courtyard:
    a part legalized outside the macro keeps only ``clearance`` from the
    courtyard edge, so that copper would end up closer than ``c_cu``."""
    import hashlib

    import numpy as np

    from pnr.place import hull as H

    ox, oy = origin
    g = float(grid_mm)
    relief = min(float(clearance), geo.c_cu)
    bw = int(math.ceil((courtyard[0] + clearance) / g))
    bh = int(math.ceil((courtyard[1] + clearance) / g))
    local = {}
    for side in PLANES:
        sh = geo.shapes.get(side) or dict(rect=[], cap=[])
        local[side] = dict(
            rect=[
                [round(x0 - ox, 6), round(y0 - oy, 6), round(x1 - ox, 6), round(y1 - oy, 6)]
                for x0, y0, x1, y1 in sh["rect"]
            ],
            cap=[
                [
                    round(x0 - ox, 6),
                    round(y0 - oy, 6),
                    round(x1 - ox, 6),
                    round(y1 - oy, 6),
                    round(r - relief, 6),
                ]
                for x0, y0, x1, y1, r in sh["cap"]
            ],
        )
    hw_, hh_ = courtyard[0] / 2.0, courtyard[1] / 2.0
    for side in local.values():
        for x0, y0, x1, y1, r in side["cap"]:
            if (
                min(x0, x1) - r < -hw_ - 1e-6
                or max(x0, x1) + r > hw_ + 1e-6
                or min(y0, y1) - r < -hh_ - 1e-6
                or max(y0, y1) + r > hh_ + 1e-6
            ):
                raise ValueError(
                    "block copper within c_cu - clearance of the macro courtyard edge "
                    "(%.3f %.3f %.3f %.3f r %.3f)" % (x0, y0, x1, y1, r)
                )
    xe, ye = H.lattice(bw, g), H.lattice(bh, g)
    erode = max(0, int(math.ceil((math.ceil(geo.track_width / g - 1e-9) - 1) / 2)))
    hull = dict(
        v=H.HULL_VERSION,
        grid_mm=g,
        cells=[bw, bh],
        clearance=float(clearance),
        c_cu=geo.c_cu,
        cap_relief=relief,
        courtyard=[float(courtyard[0]), float(courtyard[1])],
        shapes=local,
    )
    stats = {}
    area = courtyard[0] * courtyard[1]
    for side in PLANES:
        cells = H.raster(local[side], 0.0, bw, bh, g)
        raw = int(cells.sum())
        filled = 0
        if side != "inner":
            cells, filled = H.fill_pockets(cells, erode)
        hull[side] = [[round(v, 9) for v in r] for r in H.merge_cells(cells, xe, ye)]
        # occupancy inside the courtyard (cells are g x g; the slot rim is outside it)
        stats[side] = dict(
            fraction=round(float(cells.sum()) * g * g / area, 4),
            raw_cells=raw,
            pocket_cells=filled,
            rects=len(hull[side]),
        )
    hull["stats"] = stats
    hull["key"] = hashlib.sha256(
        json.dumps(
            {
                k: hull[k]
                for k in ("v", "grid_mm", "cells", "clearance", "shapes", "top", "bottom", "inner")
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    for side in ("top", "bottom"):
        boxes, cover = H.cover_boxes(hull, side)
        stats[side].update(bodies=len(boxes), body_cover=round(cover, 4))
    return hull


# ------------------------------------------------------------------ library ranking


def _instance_components(d: Path):
    from pnr.graph import BoardGraph

    for name in ("evaluated-placed.json", "placed.json"):
        p = d / name
        if p.exists():
            return BoardGraph.from_json(p.read_text()).components
    raise FileNotFoundError(f"{d}: no evaluated-placed.json / placed.json")


def _instance_rules(d: Path):
    for name in ("evaluated-rules.json", "rules.json"):
        p = d / name
        if p.exists():
            return json.loads(p.read_text())
    return {}


def used_extents(rec: dict) -> Dict[str, list]:
    """{instance name: [x0, y0, x1, y1, area]} of a library record (apron-shifted
    instance frame), raising when any instance cannot be measured."""
    out = {}
    instances = [i for i in rec.get("instances") or [] if isinstance(i, dict)]
    if not instances:
        raise ValueError("record has no instances")
    for inst in instances:
        d = inst.get("dir")
        if not d:
            raise ValueError(f"instance {inst.get('instance')} has no routed board dir")
        d = Path(d)
        geo = block_geometry(
            _instance_components(d), d / "electrical" / "board.kicad_pcb", _instance_rules(d)
        )
        x0, y0, x1, y1 = geo.extent
        out[inst.get("instance") or str(d)] = [
            round(x0, 4),
            round(y0, 4),
            round(x1, 4),
            round(y1, 4),
            round(geo.area, 4),
        ]
    return out


def used_area(rec: dict) -> float:
    """Summed used-extent area (mm^2) over the record's instances, inf when unmeasurable."""
    try:
        return float(sum(v[4] for v in used_extents(rec).values()))
    except Exception:
        return math.inf


def rank_used_q() -> float:
    return float(os.environ.get("PNR_LIBRARY_RANK_USED_Q", "0.10"))


def stamp_used(tier: List[dict]) -> List[dict]:
    """Copies of ``tier`` with ``used`` {area_mm2, instances} and ``used_band``.

    used_band = floor((a / a_min - 1) / Q + 1e-9), Q = PNR_LIBRARY_RANK_USED_Q
    (0.10); a layout that cannot be measured gets area and band inf."""
    q = rank_used_q()
    out = []
    for r in tier:
        r = dict(r)
        try:
            ext = used_extents(r)
            area = float(sum(v[4] for v in ext.values()))
            r["used"] = dict(area_mm2=round(area, 4), instances=ext)
        except Exception as error:
            area = math.inf
            r["used"] = dict(area_mm2=None, error=f"{type(error).__name__}: {error}")
        out.append((r, area))
    finite = [a for _, a in out if math.isfinite(a) and a > 0]
    a_min = min(finite) if finite else None
    for r, a in out:
        r["used_band"] = (
            int(math.floor((a / a_min - 1.0) / q + 1e-9))
            if a_min is not None and math.isfinite(a)
            else None
        )
    return [r for r, _ in out]
