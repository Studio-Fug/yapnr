"""Native KiCad geometry -> live viewer engine coordinates (mm, y-up); runs under KiCad's Python.

Usage: ``<kicad python> extract.py <board.kicad_pcb> <out.json>``, with ``PYTHONPATH`` set to the
engine runtime only (the viewer's ``runtime.kicad_env``). Stdlib plus ``pcbnew`` and
``pnr.ingest``; nothing GUI-bound; 3.9-parseable. It uses the engine's private
``pnr.ingest._board_frame`` (the board frame the engine places in), to be made public in PR3b.
"""

import json
import math
import sys


def arc_sweep(t):
    """An arc track's signed sweep in radians, start to end.

    KiCad 10 names it ``PCB_ARC.GetAngle()``; ``GetArcAngle()`` is only kept as a fallback for
    other versions (it does not exist in KiCad 10, which made every board with arc tracks fail).
    """
    get = getattr(t, "GetAngle", None) or getattr(t, "GetArcAngle")
    return get().AsRadians()


def tracks_and_vias(k, b, xy):
    tracks = []
    vias = []
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias.append(
                dict(net=t.GetNetname(), xy=xy(t.GetPosition()), diameter=t.GetWidth(k.F_Cu) / 1e6)
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
            tracks.append([t.GetNetname(), t.GetLayerName(), a, c, t.GetWidth() / 1e6])
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

    def xy(p):
        return list(frame.point(p.x, p.y))

    tracks, vias = tracks_and_vias(k, b, xy)
    doc = dict(
        frame="mm-y-up",
        width=outline.width,
        height=outline.height,
        parts=parts_of(k, b, xy),
        tracks=tracks,
        vias=vias,
        zones=zones_of(k, b, xy),
    )
    with open(argv[2], "w") as out:
        json.dump(doc, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
