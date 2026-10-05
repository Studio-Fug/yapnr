"""Copy routed block copper into a full board at each block's placed pose (KiCad python).

    python -m pnr.hier.assemble full.kicad_pcb --block block.kicad_pcb [--block ...] --out out.kicad_pcb

For each block board, the rigid transform is solved from the footprints it
shares with the full board (rotation from orientation difference, translation
from positions) and checked against every shared footprint. Tracks and vias are
cloned, transformed and re-bound to the destination net by name. Zones are not
copied by default: the full board pours its own planes.

``--zones`` (opt-in, E2): also clones each block's zones (copper pours and rule
areas), by the same rigid transform, re-bound to the destination net by name.
Undeclared, output is byte-identical to before.

``--group NAME [--anchor REF]`` (opt-in, E2, the hier -> fixed_block bridge): puts
every item this run clones (tracks, vias, and zones with ``--zones``) and every
block footprint except ``--anchor`` into a new KiCad group ``NAME`` on the full
board, and reports that group's copper digest (``pnr.fixed_copper.block_digest``,
the anchor's frame). That group is exactly what a ``fixed_block`` constraint
entry (pnr-inputs.md, ``group: NAME``, ``anchor: REF``) names: a chosen
hierarchical block layout, including its zones, turned into copper the engine
holds fixed and reserves on later placement and routing passes, instead of loose
copper the full board's own planes and router may disturb. ``REF`` itself is not
added to the group: it keeps its placed pose and the fixed_block's ``refs`` ride
on it (pnr.fixed_block, pnr.hier.macro.fixed_block_from_macro derives the two
constraint entries from the macro that chose this layout).
"""

import argparse
import json
import math


def _xf_zone(full, blk, zone, xf):
    """Clone zone ``zone`` of block board ``blk`` onto ``full``: its layer set (by
    layer name, both boards share the stack), net (by name), rule-area flags or
    pour settings, outline and holes transformed by ``xf``. Returns the new zone,
    added to ``full`` (not filled; a later plane pass fills it)."""
    import pcbnew as k

    net = full.FindNet(zone.GetNetname()) if zone.GetNetCode() else None
    if zone.GetNetCode() and net is None:
        raise SystemExit("unknown net %s" % zone.GetNetname())
    nz = k.ZONE(full)
    lset = k.LSET()
    for la in blk.GetEnabledLayers().CuStack():
        if zone.IsOnLayer(la):
            lset.AddLayer(full.GetLayerID(blk.GetLayerName(la)))
    nz.SetLayerSet(lset)
    nz.SetNetCode(net.GetNetCode() if net else 0)
    if zone.GetIsRuleArea():
        nz.SetIsRuleArea(True)
        nz.SetDoNotAllowTracks(zone.GetDoNotAllowTracks())
        nz.SetDoNotAllowVias(zone.GetDoNotAllowVias())
        nz.SetDoNotAllowZoneFills(zone.GetDoNotAllowZoneFills())
        nz.SetDoNotAllowPads(zone.GetDoNotAllowPads())
        nz.SetDoNotAllowFootprints(zone.GetDoNotAllowFootprints())
    else:
        nz.SetZoneName(zone.GetZoneName())
        nz.SetLocalClearance(zone.GetLocalClearance())
        nz.SetMinThickness(zone.GetMinThickness())
        nz.SetAssignedPriority(zone.GetAssignedPriority())
    src, outline = zone.Outline(), nz.Outline()
    for i in range(src.OutlineCount()):
        chain = src.Outline(i)
        outline.NewOutline()
        for n in range(chain.PointCount()):
            p = chain.CPoint(n)
            outline.Append(k.VECTOR2I(*xf(p.x, p.y)))
        for h in range(src.HoleCount(i)):
            hole = src.Hole(i, h)
            index = outline.NewHole()
            for n in range(hole.PointCount()):
                p = hole.CPoint(n)
                outline.Append(k.VECTOR2I(*xf(p.x, p.y)), -1, index)
    full.Add(nz)
    nz.thisown = False
    return nz


def main():
    import pcbnew

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("full")
    ap.add_argument("--block", action="append", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--tolerance-nm", type=int, default=2000)
    ap.add_argument(
        "--zones",
        action="store_true",
        help="also clone each block's zones (pours and rule areas); default drops them",
    )
    ap.add_argument(
        "--group",
        help="group the items this run clones, and every block footprint except "
        "--anchor, into a new KiCad group of this name (the fixed_block bridge)",
    )
    ap.add_argument(
        "--anchor",
        help="--group: the block footprint that stays out of the group (its "
        "placed pose is the fixed_block's frame); optional, as fixed_block's own",
    )
    a = ap.parse_args()
    if a.anchor and not a.group:
        raise SystemExit("--anchor needs --group")
    full = pcbnew.LoadBoard(a.full)
    dest = {
        fp.GetReference(): (fp.GetPosition(), fp.GetOrientationDegrees(), fp.IsFlipped())
        for fp in full.GetFootprints()
    }
    report = []
    grouped_items = []
    group_refs = set()
    for path in a.block:
        blk = pcbnew.LoadBoard(path)
        shared = [
            (fp.GetReference(), fp.GetPosition(), fp.GetOrientationDegrees(), fp.IsFlipped())
            for fp in blk.GetFootprints()
        ]
        ref0, p0, o0, f0 = shared[0]
        q0, d0, g0 = dest[ref0]
        if f0 != g0:
            raise SystemExit("side differs for %s" % ref0)
        theta = math.radians((d0 - o0) % 360)
        # KiCad y points down; orientation is counter-clockwise on screen, which is
        # clockwise in board coordinates.
        c, s = math.cos(-theta), math.sin(-theta)

        def xf(x, y):
            return (
                q0.x + round((x - p0.x) * c - (y - p0.y) * s),
                q0.y + round((x - p0.x) * s + (y - p0.y) * c),
            )

        worst = 0
        for ref, p, o, f in shared:
            q, d, g = dest[ref]
            x, y = xf(p.x, p.y)
            worst = max(worst, abs(x - q.x), abs(y - q.y))
            if f != g or abs(((d - o) - (d0 - o0) + 180) % 360 - 180) > 1e-6:
                raise SystemExit("non-rigid pose for %s" % ref)
        if worst > a.tolerance_nm:
            raise SystemExit("block %s not rigid: %d nm" % (path, worst))
        added = 0
        for t in list(blk.GetTracks()):
            net = full.FindNet(t.GetNetname()) if t.GetNetCode() else None
            if t.GetNetCode() and net is None:
                raise SystemExit("unknown net %s" % t.GetNetname())
            if t.GetClass() == "PCB_VIA":
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
            elif t.GetClass() == "PCB_TRACK":
                item = pcbnew.PCB_TRACK(full)
                item.SetStart(pcbnew.VECTOR2I(*xf(t.GetStart().x, t.GetStart().y)))
                item.SetEnd(pcbnew.VECTOR2I(*xf(t.GetEnd().x, t.GetEnd().y)))
                item.SetLayer(t.GetLayer())
                item.SetWidth(t.GetWidth())
            else:
                raise SystemExit("unsupported copper %s" % t.GetClass())
            item.SetNetCode(net.GetNetCode() if net else 0)
            full.Add(item)
            item.thisown = False
            added += 1
            grouped_items.append(item)
        block_report = dict(block=path, footprints=len(shared), items=added, rigid_error_nm=worst)
        if a.zones:
            zones = 0
            for z in list(blk.Zones()):
                item = _xf_zone(full, blk, z, xf)
                grouped_items.append(item)
                zones += 1
            block_report["zones"] = zones
        report.append(block_report)
        group_refs.update(ref for ref, _, _, _ in shared if ref != a.anchor)
    full.BuildConnectivity()
    out = report
    if a.group:
        if any(g.GetName() == a.group for g in full.Groups()):
            raise SystemExit("group %r already exists on %s" % (a.group, a.full))
        from pnr.fixed_copper import block_digest

        group = pcbnew.PCB_GROUP(full)
        group.SetName(a.group)
        full.Add(group)
        for item in grouped_items:
            group.AddItem(item)
        for ref in sorted(group_refs):
            fp = full.FindFootprintByReference(ref)
            if fp is None:
                raise SystemExit("block footprint %s is not on %s" % (ref, a.full))
            group.AddItem(fp)
        digest = block_digest(full, a.group, a.anchor)
        out = dict(
            blocks=report,
            group=a.group,
            anchor=a.anchor,
            refs=sorted(group_refs),
            sha256=digest,
        )
    pcbnew.SaveBoard(a.out, full)
    print(json.dumps(out), flush=True)
    import os

    os._exit(0)


if __name__ == "__main__":
    main()
