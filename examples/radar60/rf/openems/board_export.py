"""Export the copper of a zone-filled KiCad board to JSON in U1's frame (KiCad python).

  <KiCad python> openems/board_export.py BOARD.kicad_pcb OUT.json X0 Y0   (macro boards: 100 100)

U1 frame: x = xk - X0, y = Y0 - yk (mm, +y north). Exports F.Cu and In1.Cu filled zone
polygons (outline + holes), tracks and arcs (start, mid, end, width, net), pads (polygon), vias
(x, y, pad, drill, net).
"""

import json
import sys

import pcbnew

src, out, X0, Y0 = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
b = pcbnew.LoadBoard(src)
tm = pcbnew.ToMM


def P(v):
    return [round(tm(v.x) - X0, 6), round(Y0 - tm(v.y), 6)]


def chain(c):
    return [P(c.CPoint(k)) for k in range(c.PointCount())]


def polyset(ps):
    out = []
    for i in range(ps.OutlineCount()):
        out.append([chain(ps.Outline(i)), [chain(ps.Hole(i, j)) for j in range(ps.HoleCount(i))]])
    return out


LAYERS = {"F.Cu": pcbnew.F_Cu, "In1.Cu": pcbnew.In1_Cu}
d = dict(fill=[], tracks=[], pads=[], vias=[])
for z in b.Zones():
    if z.GetIsRuleArea():
        continue
    for name, lid in LAYERS.items():
        if not z.GetLayerSet().Contains(lid):
            continue
        ps = z.GetFilledPolysList(lid)
        if ps.OutlineCount():
            d["fill"].append(dict(layer=name, net=z.GetNetname(), poly=polyset(ps)))
for t in b.GetTracks():
    if t.GetClass() == "PCB_VIA":
        d["vias"].append(
            [
                *P(t.GetPosition()),
                tm(t.GetWidth(pcbnew.F_Cu)),
                tm(t.GetDrillValue()),
                t.GetNetname(),
            ]
        )
        continue
    rec = dict(
        net=t.GetNetname(),
        layer=b.GetLayerName(t.GetLayer()),
        kind=t.GetClass(),
        start=P(t.GetStart()),
        end=P(t.GetEnd()),
        w=tm(t.GetWidth()),
    )
    if t.GetClass() == "PCB_ARC":
        rec["mid"] = P(t.GetMid())
    d["tracks"].append(rec)
for f in b.GetFootprints():
    for p in f.Pads():
        for name, lid in LAYERS.items():
            if not p.IsOnLayer(lid):
                continue
            ps = pcbnew.SHAPE_POLY_SET()
            p.TransformShapeToPolygon(ps, lid, 0, pcbnew.FromMM(0.002), pcbnew.ERROR_INSIDE)
            d["pads"].append(
                dict(
                    ref=f.GetReference(),
                    num=p.GetNumber(),
                    net=p.GetNetname(),
                    layer=name,
                    poly=polyset(ps),
                )
            )
with open(out, "w") as fh:
    json.dump(d, fh)
print(out, {k: len(v) for k, v in d.items()})
