"""Independent placement-constraint checker for any tool's routed KiCad board.

Reads the saved ``.kicad_pcb`` with KiCad's own Python (pcbnew), never the engine's
outputs, and verifies a rung's tool-neutral ``checks`` list (hard_rungs.py), one
verdict per check with the measured value:

    satisfied | violated | error

Frame: millimetres from the board outline's lower-left corner, y up (the ladder's
engine frame); a part's position is its footprint origin and its rotation KiCad's
footprint orientation. Courtyards are the footprint's own courtyard layer (the
bottom courtyard for a flipped part), as an axis-aligned box.

Check kinds: ``inside_board``, ``side``, ``fixed``, ``edge``, ``orientation``,
``keepout``, ``region``, ``proximity``, ``line``, ``align``, ``plane``.

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

    def rot(self, ref):
        return self.fps[ref].GetOrientationDegrees() % 360

    def side(self, ref):
        return "bottom" if self.fps[ref].IsFlipped() else "top"

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
    anchor = b.pos(c["anchor"])
    dists = {r: round(math.dist(anchor, b.pos(r)), 3) for r in c["refs"]}
    worst = max(dists.values())
    return worst <= c["max_mm"] + TOL, dict(distance_mm=dists), dict(max_mm=c["max_mm"])


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
    align=check_align,
    plane=check_plane,
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
