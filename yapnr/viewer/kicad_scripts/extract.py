"""Native KiCad geometry -> live viewer engine coordinates (mm, y-up); runs under KiCad's Python.

Usage: ``<kicad python> extract.py <board.kicad_pcb> <out.json>``, with ``PYTHONPATH`` set to the
engine runtime only (the viewer's ``runtime.kicad_env``). Stdlib plus ``pcbnew`` and
``pnr.ingest``; nothing GUI-bound; 3.9-parseable. It uses the engine's private
``pnr.ingest._board_frame`` (the board frame the engine places in), to be made public in PR3b.
"""

import json
import math
import sys


def minimum_airwires(groups):
    """Minimum spanning tree of disconnected groups, using their copper anchors.

    Connected copper is already contracted into one vertex by native KiCad.
    Deterministic Prim selection includes only missing links, never an MST of
    all pads (which would redraw connections already supplied by copper).
    """
    if len(groups) < 2:
        return []
    groups = [sorted(set(tuple(p) for p in group)) for group in groups]
    remaining = set(range(1, len(groups)))
    best = {}
    result = []
    current = 0
    while remaining:
        for i in sorted(remaining):
            candidate = min(
                ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2, a, b)
                for a in groups[current]
                for b in groups[i]
            )
            if i not in best or candidate < best[i]:
                best[i] = candidate
        current = min(remaining, key=lambda i: (best[i], i))
        _, a, b = best[current]
        result.append([list(a), list(b)])
        remaining.remove(current)
    return result


def airwires_of(board, xy):
    """Native copper connectivity, including vias and actual filled zone islands.

    KiCad's RN_NET edges are opaque in SWIG. Use its transitive connectivity to
    contract pad-bearing islands, then minimize links between pad/via centres
    and track endpoints. This is an anchor-distance guide, not a route or a DRC
    result. Never infer connectivity from overlapping XY positions or net names.
    """
    connectivity = board.GetConnectivity()
    connectivity.Build(board)
    groups = {}
    seen = set()
    pads = sorted(
        (p for f in board.GetFootprints() for p in f.Pads() if p.GetNetCode() > 0),
        key=lambda p: p.m_Uuid.AsString(),
    )
    for pad in pads:
        if pad.m_Uuid.AsString() in seen:
            continue
        items = [pad] + list(connectivity.GetConnectedItems(pad))
        anchors = set()
        for item in items:
            if item.GetNetCode() != pad.GetNetCode():
                continue
            kind = item.GetClass()
            if kind == "PAD":
                seen.add(item.m_Uuid.AsString())
            if kind in ("PAD", "PCB_VIA"):
                anchors.add(tuple(xy(item.GetPosition())))
            elif kind in ("PCB_TRACK", "PCB_ARC"):
                anchors.update((tuple(xy(item.GetStart())), tuple(xy(item.GetEnd()))))
        groups.setdefault(pad.GetNetname(), []).append(anchors)
    return [
        dict(net=net, a=a, b=b)
        for net, islands in sorted(groups.items())
        for a, b in minimum_airwires(islands)
    ]


def arc_sweep(t):
    """An arc track's signed sweep in radians, start to end.

    KiCad 10 names it ``PCB_ARC.GetAngle()``; ``GetArcAngle()`` is only kept as a fallback for
    other versions (it does not exist in KiCad 10, which made every board with arc tracks fail).
    """
    get = getattr(t, "GetAngle", None) or getattr(t, "GetArcAngle")
    return get().AsRadians()


def display_bounds(board, outline):
    """Show an unplaced board's real object bounds without inventing Edge.Cuts."""
    if outline is not None:
        return outline.width, outline.height, True
    box = board.ComputeBoundingBox(False)
    return box.GetWidth() / 1e6, box.GetHeight() / 1e6, False


def copper_layers(k, board):
    """Enabled copper in physical order, including layers with no geometry."""
    return [k.BOARD.GetStandardLayerName(layer) for layer in board.GetEnabledLayers().CuStack()]


def tracks_and_vias(k, b, xy):
    tracks = []
    vias = []
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias.append(
                dict(
                    net=t.GetNetname(),
                    xy=xy(t.GetPosition()),
                    diameter=t.GetWidth(k.F_Cu) / 1e6,
                    layers=[
                        k.BOARD.GetStandardLayerName(layer)
                        for layer in b.GetEnabledLayers().CuStack()
                        if t.IsOnLayer(layer)
                    ],
                )
            )
            continue
        if t.GetClass() == "PCB_ARC":
            center = t.GetCenter()
            start = t.GetStart()
            angle = arc_sweep(t)
            radius = math.hypot(start.x - center.x, start.y - center.y)
            theta = math.atan2(start.y - center.y, start.x - center.x)
            pts = [
                xy(
                    k.VECTOR2I(
                        int(center.x + radius * math.cos(theta + angle * i / 32)),
                        int(center.y + radius * math.sin(theta + angle * i / 32)),
                    )
                )
                for i in range(33)
            ]
        else:
            pts = [xy(t.GetStart()), xy(t.GetEnd())]
        for a, c in zip(pts, pts[1:]):
            tracks.append(
                [
                    t.GetNetname(),
                    k.BOARD.GetStandardLayerName(t.GetLayer()),
                    a,
                    c,
                    t.GetWidth() / 1e6,
                ]
            )
    return tracks, vias


def parts_of(k, b, xy):
    parts = []
    for f in b.GetFootprints():
        pads = []
        for p in f.Pads():
            cu = [layer for layer in p.GetLayerSet().Seq() if k.IsCopperLayer(layer)]
            pad = dict(
                number=p.GetNumber(),
                net=p.GetNetname(),
                xy=xy(p.GetPosition()),
                size=[p.GetSize().x / 1e6, p.GetSize().y / 1e6],
                angle=p.GetOrientationDegrees(),
                shape="circle" if p.GetShape() == k.PAD_SHAPE_CIRCLE else "rect",
                layers=[k.BOARD.GetStandardLayerName(layer) for layer in cu],
            )
            if cu:
                # Real copper outline (custom/L-shaped, round-rect, oval, chamfered pads); size
                # alone is only the anchor for custom pads (e.g. 0.01 mm), which rendered as a
                # speck.
                ps = k.SHAPE_POLY_SET()
                p.TransformShapeToPolygon(
                    ps, cu[0], 0, b.GetDesignSettings().m_MaxError, k.ERROR_INSIDE
                )
                pad["polys"] = [
                    [xy(ps.Outline(j).CPoint(i)) for i in range(ps.Outline(j).PointCount())]
                    for j in range(ps.OutlineCount())
                ]
                if p.GetShape() == k.PAD_SHAPE_CUSTOM:
                    bb = ps.BBox()
                    pad.update(
                        size=[bb.GetWidth() / 1e6, bb.GetHeight() / 1e6],
                        angle=0.0,
                        xy=xy(bb.Centre()),
                    )
            pads.append(pad)
        parts.append(dict(ref=f.GetReference(), xy=xy(f.GetPosition()), pads=pads))
    return parts


def zones_of(k, b, xy):
    zones = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        for layer in z.GetLayerSet().Seq():
            if not k.IsCopperLayer(layer):
                continue
            try:
                poly = z.GetFilledPolysList(layer)
                for j in range(poly.OutlineCount()):
                    line = poly.COutline(j)
                    paths = [[xy(line.CPoint(i)) for i in range(line.PointCount())]]
                    for h in range(poly.HoleCount(j)):
                        hole = poly.CHole(j, h)
                        paths.append([xy(hole.CPoint(i)) for i in range(hole.PointCount())])
                    zones.append(
                        dict(
                            layer=k.BOARD.GetStandardLayerName(layer),
                            net=z.GetNetname(),
                            paths=paths,
                        )
                    )
            except Exception as e:
                raise RuntimeError("Cannot extract native plane polygons: " + str(e))
    return zones


def main(argv):
    import pcbnew as k

    from pnr.ingest import _board_frame

    b = k.LoadBoard(argv[1])
    frame, outline = _board_frame(b)
    width, height, outline_present = display_bounds(b, outline)

    def xy(p):
        return list(frame.point(p.x, p.y))

    tracks, vias = tracks_and_vias(k, b, xy)
    doc = dict(
        frame="mm-y-up",
        copper_layers=copper_layers(k, b),
        width=width,
        height=height,
        outline_present=outline_present,
        parts=parts_of(k, b, xy),
        tracks=tracks,
        vias=vias,
        zones=zones_of(k, b, xy),
        airwires=airwires_of(b, xy),
    )
    with open(argv[2], "w") as out:
        json.dump(doc, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
