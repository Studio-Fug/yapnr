"""Copy routed block copper into a full board at each block's placed pose (KiCad python).

    python -m pnr.hier.assemble full.kicad_pcb --block block.kicad_pcb [--block ...] --out out.kicad_pcb

For each block board, the rigid transform is solved from the footprints it
shares with the full board (rotation from orientation difference, translation
from positions) and checked against every shared footprint. Tracks and vias are
cloned, transformed and re-bound to the destination net by name. Zones are not
copied; the full board pours its own planes.
"""
import argparse
import json
import math


def main():
    import pcbnew
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('full')
    ap.add_argument('--block', action='append', default=[])
    ap.add_argument('--out', required=True)
    ap.add_argument('--tolerance-nm', type=int, default=2000)
    a = ap.parse_args()
    full = pcbnew.LoadBoard(a.full)
    dest = {fp.GetReference(): (fp.GetPosition(), fp.GetOrientationDegrees(), fp.IsFlipped())
            for fp in full.GetFootprints()}
    report = []
    for path in a.block:
        blk = pcbnew.LoadBoard(path)
        shared = [(fp.GetReference(), fp.GetPosition(), fp.GetOrientationDegrees(), fp.IsFlipped())
                  for fp in blk.GetFootprints()]
        ref0, p0, o0, f0 = shared[0]
        q0, d0, g0 = dest[ref0]
        if f0 != g0:
            raise SystemExit('side differs for %s' % ref0)
        theta = math.radians((d0 - o0) % 360)
        # KiCad y points down; orientation is counter-clockwise on screen, which is
        # clockwise in board coordinates.
        c, s = math.cos(-theta), math.sin(-theta)

        def xf(x, y):
            return (q0.x + round((x - p0.x) * c - (y - p0.y) * s),
                    q0.y + round((x - p0.x) * s + (y - p0.y) * c))
        worst = 0
        for ref, p, o, f in shared:
            q, d, g = dest[ref]
            x, y = xf(p.x, p.y)
            worst = max(worst, abs(x - q.x), abs(y - q.y))
            if f != g or abs(((d - o) - (d0 - o0) + 180) % 360 - 180) > 1e-6:
                raise SystemExit('non-rigid pose for %s' % ref)
        if worst > a.tolerance_nm:
            raise SystemExit('block %s not rigid: %d nm' % (path, worst))
        added = 0
        for t in list(blk.GetTracks()):
            net = full.FindNet(t.GetNetname()) if t.GetNetCode() else None
            if t.GetNetCode() and net is None:
                raise SystemExit('unknown net %s' % t.GetNetname())
            if t.GetClass() == 'PCB_VIA':
                v = pcbnew.PCB_VIA(full)
                v.SetPosition(pcbnew.VECTOR2I(*xf(t.GetPosition().x, t.GetPosition().y)))
                v.SetViaType(t.GetViaType())
                v.SetLayerPair(t.TopLayer(), t.BottomLayer())
                v.SetDrill(t.GetDrillValue())
                for la in full.GetEnabledLayers().CuStack():
                    v.SetWidth(la, t.GetWidth(la))
                # Filled in-pad vias drop unused inner pads (5B); legacy vias keep all.
                v.Padstack().SetUnconnectedLayerMode(t.Padstack().UnconnectedLayerMode())
                item = v
            elif t.GetClass() == 'PCB_TRACK':
                item = pcbnew.PCB_TRACK(full)
                item.SetStart(pcbnew.VECTOR2I(*xf(t.GetStart().x, t.GetStart().y)))
                item.SetEnd(pcbnew.VECTOR2I(*xf(t.GetEnd().x, t.GetEnd().y)))
                item.SetLayer(t.GetLayer())
                item.SetWidth(t.GetWidth())
            else:
                raise SystemExit('unsupported copper %s' % t.GetClass())
            item.SetNetCode(net.GetNetCode() if net else 0)
            full.Add(item)
            item.thisown = False
            added += 1
        report.append(dict(block=path, footprints=len(shared), items=added, rigid_error_nm=worst))
    full.BuildConnectivity()
    pcbnew.SaveBoard(a.out, full)
    print(json.dumps(report), flush=True)
    import os
    os._exit(0)


if __name__ == '__main__':
    main()
