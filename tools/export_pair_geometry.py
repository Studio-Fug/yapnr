#!/usr/bin/env python3
"""Export saved native board copper for animate_pair_access.py.

Run under the configured headless KiCad Python; never creates a wx application.
"""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import pcbnew as k

    def mm(value):
        return value / 1e6

    def xy(point):
        return [mm(point.x), mm(point.y)]

    b = k.LoadBoard(str(args.board))
    data = {"tracks": [], "vias": [], "pads": [], "footprints": [], "edges": []}
    for t in b.GetTracks():
        if isinstance(t, k.PCB_VIA):
            data["vias"].append(
                dict(
                    net=t.GetNetname(),
                    pos=xy(t.GetPosition()),
                    diameter=mm(t.GetWidth(k.F_Cu)),
                    drill=mm(t.GetDrillValue()),
                )
            )
        else:
            data["tracks"].append(
                dict(
                    net=t.GetNetname(),
                    layer=b.GetLayerName(t.GetLayer()),
                    a=xy(t.GetStart()),
                    z=xy(t.GetEnd()),
                    width=mm(t.GetWidth()),
                    uuid=t.m_Uuid.AsString(),
                )
            )
    for f in b.GetFootprints():
        bb = f.GetBoundingBox(False, False)
        data["footprints"].append(
            dict(
                ref=f.GetReference(),
                value=f.GetValue(),
                pos=xy(f.GetPosition()),
                bbox=[mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())],
            )
        )
        for p in f.Pads():
            lands = [i for i in b.GetEnabledLayers().CuStack() if p.IsOnLayer(i)]
            if not lands:
                continue
            poly = p.GetEffectivePolygon(lands[0])
            outline = poly.COutline(0)
            pts = [xy(outline.CPoint(i)) for i in range(outline.PointCount())]
            data["pads"].append(
                dict(
                    ref=f.GetReference(),
                    number=p.GetNumber(),
                    net=p.GetNetname(),
                    pos=xy(p.GetPosition()),
                    outline=pts,
                    layers=[b.GetLayerName(i) for i in [k.F_Cu, k.B_Cu] if p.IsOnLayer(i)],
                    side=b.GetLayerName(f.GetLayer()),
                    drill=xy(p.GetDrillSize()),
                )
            )
    args.out.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
