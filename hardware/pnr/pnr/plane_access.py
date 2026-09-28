"""Local surface groups and deterministic, source-sized plane-access arrays."""

import math, os
from pnr.plane_intent import size_array

# Existing-bank reuse: barrel positions may differ by assembly rounding (a few nm..um).
MATCH_TOL_MM = .01
SIZE_TOL_MM = .001


def uid(t):
    return t.m_Uuid.AsString()


def matching_array(planned, existing, pitch, tol_mm=MATCH_TOL_MM, size_tol_mm=SIZE_TOL_MM):
    """Keys of the existing via group that already is the planned bank, else None.

    planned: [((x,y),diameter,drill)] in mm; existing: [(key,(x,y),diameter,drill,through)]
    for the vias of the bank's net. The group is every existing via chained to a
    planned position at <= pitch spacing, so an extra, missing, shifted, resized or
    non-through barrel is a different bank. Pure python; no pcbnew.
    """
    if not planned:return None
    def near(a,b,d):return math.dist(a,b)<=d+1e-9
    group={e[0]:e for e in existing if any(near(e[1],p,tol_mm) for p,_,_ in planned)}
    todo=list(group.values())
    while todo:
        x=todo.pop()
        for e in existing:
            if e[0] not in group and near(x[1],e[1],pitch+tol_mm):group[e[0]]=e;todo.append(e)
    if len(group)!=len(planned):return None
    keys=[]
    for point,diameter,drill in planned:
        hits=[e for e in group.values() if near(e[1],point,tol_mm)]
        if len(hits)!=1 or hits[0][0] in keys:return None
        key,_,d,h,through=hits[0]
        if not through or abs(d-diameter)>size_tol_mm or abs(h-drill)>size_tol_mm:return None
        keys.append(key)
    return keys


def existing_array(board, net, geometry, fab, tol_mm=MATCH_TOL_MM):
    """Board vias on net that already form geometry['vias'] (matching_array), else None."""
    import pcbnew
    vias=[t for t in board.GetTracks() if t.GetClass()=='PCB_VIA' and t.GetNetCode()==net]
    s=geometry['sizing'];pitch=max(s['diameter_mm']+.2,s['drill_mm']+fab.get('hole_to_hole_mm',fab.get('hole_clearance_mm',.2)))
    keys=matching_array(geometry['vias'],[(i,(v.GetPosition().x/1e6,v.GetPosition().y/1e6),
                         v.GetWidth(pcbnew.F_Cu)/1e6,v.GetDrillValue()/1e6,
                         v.GetViaType()==pcbnew.VIATYPE_THROUGH and v.TopLayer()==pcbnew.F_Cu
                         and v.BottomLayer()==pcbnew.B_Cu) for i,v in enumerate(vias)],pitch,tol_mm)
    return None if keys is None else [vias[i] for i in keys]


def covering_tracks(planned, segments, tol_mm=MATCH_TOL_MM, size_tol_mm=SIZE_TOL_MM):
    """Keys of the straight segments that cover every planned track, else None.

    planned: [((x,y),(x,y),width)] in mm; segments: [(key,(x,y),(x,y),width)] on the
    bank's net and surface. A planned track is covered by the union of segments at
    least as wide whose ends lie within tol_mm of its centreline and which span it end
    to end with no gap over tol_mm, so a missing, narrowed or offset bus/feed is not
    the planned bank. Pure python; no pcbnew.
    """
    keys=[]
    for a,b,width in planned:
        length=math.dist(a,b)
        if length<=tol_mm:continue
        ux,uy=(b[0]-a[0])/length,(b[1]-a[1])/length
        spans=[]
        for key,s,e,w in segments:
            if w<width-size_tol_mm or any(abs((p[0]-a[0])*uy-(p[1]-a[1])*ux)>tol_mm+1e-9 for p in (s,e)):continue
            ts=[(p[0]-a[0])*ux+(p[1]-a[1])*uy for p in (s,e)];spans.append((min(ts),max(ts),key))
        reach=0.0
        for lo,hi,key in sorted(spans,key=lambda x:x[:2]):
            if reach>=length-tol_mm-1e-9 or lo>reach+tol_mm+1e-9:break
            if hi>reach:reach=hi;keys.append(key)
        if reach<length-tol_mm-1e-9:return None
    return list(dict.fromkeys(keys))


def existing_tracks(board, net, geometry):
    """Board F.Cu tracks on net that cover geometry['tracks'] (covering_tracks), else None."""
    import pcbnew
    def mm(p):return (p.x/1e6,p.y/1e6)
    tracks=[t for t in board.GetTracks() if t.GetClass()=='PCB_TRACK' and t.GetNetCode()==net
            and t.GetLayer()==pcbnew.F_Cu]
    keys=covering_tracks(geometry['tracks'],[(i,mm(t.GetStart()),mm(t.GetEnd()),t.GetWidth()/1e6)
                                             for i,t in enumerate(tracks)])
    return None if keys is None else [tracks[i] for i in keys]


def _rule_conflict(board, kept, vias):
    """Kept bank copper shorts or breaks the board's own clearance/hole-to-hole rules.

    Pair gap is max(board minimum clearance, both items' rule clearance); a board
    without a rule engine (constructed in python) reports 0, i.e. physical overlap.
    """
    ds=board.GetDesignSettings();net=kept[0].GetNetCode();ids={uid(t) for t in kept}
    others=[t for t in list(board.GetTracks())+[p for f in board.GetFootprints() for p in f.Pads()]
            if uid(t) not in ids]
    # A foreign via with removed unused pads (5B in-pad) also keeps the board's
    # hole clearance from its drill wall (pnr.via_in_pad.clearance_shapes).
    from pnr.via_in_pad import clearance_shapes
    def collides(shape,t,layer,gap):
        return any(shape.Collide(other,gap) for other in clearance_shapes(t,layer,gap/1e6,ds.m_HoleClearance/1e6))
    for item in kept:
        for layer in board.GetEnabledLayers().CuStack():
            if not item.IsOnLayer(layer):continue
            shape=item.GetEffectiveShape(layer);own=max(ds.m_MinClearance,item.GetOwnClearance(layer))
            if any(t.GetNetCode()!=net and t.IsOnLayer(layer) and
                   collides(shape,t,layer,max(own,t.GetOwnClearance(layer))) for t in others):
                return True
    drilled=[t for t in others if t.GetClass()=='PCB_VIA' or (t.GetClass()=='PAD' and
             max(t.GetDrillSize().x,t.GetDrillSize().y)>0)]
    return any(v.GetEffectiveHoleShape().Collide(t.GetEffectiveHoleShape(),max(0,ds.m_HoleToHoleMin))
               for v in vias for t in drilled)


def _hole_gap(fab, other):
    """Drill gap from a new (filled) array via to an existing hole.

    Without per-kind profile keys this is hole_clearance_mm (pre-profile rule).
    """
    base=fab.get('hole_to_hole_mm',fab.get('hole_clearance_mm',.2))
    if other.GetClass()!='PAD':return base
    import pcbnew
    if other.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH:return fab.get('filled_via_hole_to_hole_mm',base)
    small=fab.get('component_pth_min_drill_mm')
    if small is not None and max(other.GetDrillSize().x,other.GetDrillSize().y)/1e6<small-1e-9:
        return base  # a footprint's via-class drill (under 5A Component PTH hole 0.30): via to via
    return fab.get('pth_hole_to_hole_mm',base)


def plan_geometry(fp, pads, intent, fab, offset_mm=0.0):
    from pnr.plane_intent import array_geometry
    def mm(point):return (point.x/1e6,point.y/1e6)
    return array_geometry([(mm(p.GetPosition()),(p.GetBoundingBox().GetWidth()/1e6,
                           p.GetBoundingBox().GetHeight()/1e6)) for p in pads],
                          mm(fp.GetPosition()),intent,fab,offset_mm)


def planned_copper(board, geometry, net):
    import pcbnew
    def vector(point):return pcbnew.VECTOR2I(*(round(x*1e6) for x in point))
    planned=[]
    for start,end,width in geometry['tracks']:
        t=pcbnew.PCB_TRACK(board);t.SetStart(vector(start));t.SetEnd(vector(end))
        t.SetLayer(pcbnew.F_Cu);t.SetWidth(round(width*1e6));t.SetNetCode(net)
        planned.append(t)
    for position,diameter,drill in geometry['vias']:
        v=pcbnew.PCB_VIA(board);v.SetPosition(vector(position))
        v.SetViaType(pcbnew.VIATYPE_THROUGH);v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu)
        v.SetFrontWidth(round(diameter*1e6));v.SetDrill(round(drill*1e6));v.SetNetCode(net)
        planned.append(v)
    return planned


def _off_board(box, bounds, edge):
    return bounds.GetWidth() and bounds.GetHeight() and not (
        box.GetLeft()>=bounds.GetLeft()+edge and box.GetRight()<=bounds.GetRight()-edge and
        box.GetTop()>=bounds.GetTop()+edge and box.GetBottom()<=bounds.GetBottom()-edge)


def _keepout(zones, item, layer, box):
    return any(z.GetIsRuleArea() and z.IsOnLayer(layer) and
               (z.GetDoNotAllowVias() if item.GetClass()=='PCB_VIA' else z.GetDoNotAllowTracks())
               and z.GetBoundingBox().Intersects(box) for z in zones)


def surface_group(board, seeds, layer, excluded=()):
    items = [p for f in board.GetFootprints() for p in f.Pads()] + list(
        board.GetTracks()
    )
    net = seeds[0].GetNetCode()
    items = [t for t in items if t.GetNetCode() == net and t.IsOnLayer(layer)
             and uid(t) not in excluded]
    reached = {uid(t) for t in seeds}
    todo = list(seeds)
    while todo:
        x = todo.pop()
        shape = x.GetEffectiveShape(layer)
        for t in items:
            if uid(t) not in reached and shape.Collide(t.GetEffectiveShape(layer), 0):
                reached.add(uid(t))
                todo.append(t)
    return [t for t in items if uid(t) in reached]


def _reuse_existing(board, fp, pads, intent, fab, offset_mm=0.0):
    """Report for an already-present planned bank that is safe to keep, else None.

    Kept only if the vias match (existing_array), every planned bus/feed is covered
    by same-net F.Cu at least as wide (existing_tracks), the pads reach every bank via
    over F.Cu, and the kept copper is in-outline, keepout-free and legal against
    foreign copper and holes at the board's own rules (_rule_conflict). Never
    mutates. Anything else (or an invalid plan) returns None so the replacement path
    runs unchanged and rebuilds or raises its own diagnostics.
    """
    import pcbnew
    from pnr.writeback import outline_bounds
    try:geometry=plan_geometry(fp,pads,intent,fab,offset_mm)
    except ValueError:return None
    net=pads[0].GetNetCode();match=existing_array(board,net,geometry,fab)
    if match is None:return None
    tracks=existing_tracks(board,net,geometry)
    if tracks is None:return None
    reached={uid(t) for t in surface_group(board,pads,pcbnew.F_Cu)}
    if not all(uid(v) in reached for v in match):return None
    kept=match+tracks
    bounds=outline_bounds(board);edge=round(fab.get('edge_clearance_mm',.2)*1e6)
    zones=list(board.Zones())+[z for f in board.GetFootprints() for z in f.Zones()]
    for item in kept:
        box=item.GetBoundingBox()
        if _off_board(box,bounds,edge) or any(item.IsOnLayer(la) and _keepout(zones,item,la,box)
                                              for la in board.GetEnabledLayers().CuStack()):return None
    if _rule_conflict(board,kept,match):return None
    return dict(geometry['sizing'],previous_vias=len(match),bus_width_mm=geometry['bus_width_mm'],
                feed_width_mm=geometry['feed_width_mm'],span_mm=geometry['span_mm'],
                ref=intent['ref'],address=intent['address'],existing_array=True)


def replace_power_array(board, intent, fab, offset_mm=0.0):
    """Replace an isolated power pad group's fanouts by a shared bus and via bank.

    Existing foreign layer ports and other package pads are not ripped up.
    An already-present identical, connected and rule-legal bank (_reuse_existing)
    is kept and reported as existing_array=True without mutation.
    Caller must validate native DRC/connectivity transactionally before accepting.
    No reference-specific dimensions or current values occur here.
    """
    import pcbnew

    fp = next(f for f in board.GetFootprints() if f.GetReference() == intent["ref"])
    pads = [p for p in fp.Pads() if p.GetNumber() in intent["pads"]]
    assert pads and len({p.GetNetCode() for p in pads}) == 1
    if intent["surface"] != "F.Cu":
        raise ValueError("power bank currently supports F.Cu only")
    if not all(p.IsOnLayer(pcbnew.F_Cu) for p in pads):
        raise ValueError("source power array pads are not on the annotated surface")
    # Idempotent: a board already carrying this exact bank (routed block copper
    # assembled before plane access, or a re-run) keeps it untouched; the fanout
    # precheck's fab gap would otherwise flag neighbours routed legally at the board
    # clearance as foreign copper. Kept copper is still checked at the board's own
    # rules. PNR_PLANE_ACCESS_REUSE_EXISTING=0 restores unconditional replacement.
    if os.environ.get('PNR_PLANE_ACCESS_REUSE_EXISTING','1')!='0':
        reused=_reuse_existing(board,fp,pads,intent,fab,offset_mm)
        if reused is not None:return reused
    group = surface_group(board, pads, pcbnew.F_Cu)
    if any(
        t.GetClass() == "PAD"
        and (
            t.GetParentFootprint().GetReference() != intent["ref"]
            or t.GetNumber() not in intent["pads"]
        )
        for t in group
    ):
        raise ValueError("power group includes additional pads; requires joint policy")
    vias = [t for t in group if t.GetClass() == "PCB_VIA"]
    for v in vias:
        for t in board.GetTracks():
            if (
                t.GetClass() != "PCB_VIA"
                and t.GetLayer() != pcbnew.F_Cu
                and v.GetEffectiveShape(t.GetLayer()).Collide(
                    t.GetEffectiveShape(t.GetLayer()), 0
                )
            ):
                raise ValueError("preserve external layer port")
    geometry=plan_geometry(fp,pads,intent,fab,offset_mm)
    sizing=geometry['sizing'];bus_width=geometry['bus_width_mm']
    feed_width=geometry['feed_width_mm'];span=geometry['span_mm']
    planned=planned_copper(board,geometry,pads[0].GetNetCode())
    from pnr.writeback import outline_bounds
    bounds=outline_bounds(board);edge=round(fab.get('edge_clearance_mm',.2)*1e6)
    gap=round(fab.get('clearance_mm',.2)*1e6)
    removed={uid(t) for t in group if t.GetClass()!='PAD'}
    obstacles=[t for t in list(board.GetTracks())+[p for f in board.GetFootprints() for p in f.Pads()]
               if uid(t) not in removed]
    zones=list(board.Zones())+[z for f in board.GetFootprints() for z in f.Zones()]
    # Foreign vias with removed unused pads (5B in-pad) also keep the fab model's
    # via hole clearance from their drill wall (pnr.via_in_pad.clearance_shapes).
    from pnr.via_in_pad import clearance_shapes
    hole_clearance=float(fab.get('hole_clearance_mm',0.0))
    for item in planned:
        box=item.GetBoundingBox()
        if _off_board(box,bounds,edge):
            raise ValueError('current-sized array crosses board edge; reserve placement space')
        for layer in board.GetEnabledLayers().CuStack():
            if not item.IsOnLayer(layer):continue
            shape=item.GetEffectiveShape(layer)
            if any(t.GetNetCode()!=item.GetNetCode() and t.IsOnLayer(layer) and
                   any(shape.Collide(other,gap) for other in clearance_shapes(t,layer,gap/1e6,hole_clearance))
                   for t in obstacles):
                raise ValueError('current-sized array collides with foreign copper; reserve routing space')
            if _keepout(zones,item,layer,box):
                raise ValueError('current-sized array crosses copper keepout')
        if item.GetClass()=='PCB_VIA' and fab.get('via_to_smd_pad_mm') is not None:
            # Profile (5A "Vias in SMD pads": via copper to SMD pad 0.127, any net):
            # array barrels sit beside the pad row and never inside a pad.
            keep=round((fab['via_to_smd_pad_mm']+.001)*1e6)
            if any(p.GetAttribute()==pcbnew.PAD_ATTRIB_SMD and p.IsOnLayer(layer) and
                   p.GetEffectiveShape(layer).Collide(item.GetEffectiveShape(layer),keep)
                   for p in obstacles if p.GetClass()=='PAD'
                   for layer in (pcbnew.F_Cu,pcbnew.B_Cu)):
                raise ValueError('current-sized array via within the via-to-SMD-pad distance; reserve routing space')
        if item.GetClass()=='PCB_VIA':
            if fab.get('hole_to_edge_mm') is not None and _off_board(
                    item.GetEffectiveHoleShape().BBox(),bounds,round(fab['hole_to_edge_mm']*1e6)):
                raise ValueError('current-sized array hole violates hole-to-edge; reserve placement space')
            for other in obstacles:
                drilled=other.GetClass()=='PCB_VIA' or (other.GetClass()=='PAD' and
                           max(other.GetDrillSize().x,other.GetDrillSize().y)>0)
                if drilled and item.GetEffectiveHoleShape().Collide(other.GetEffectiveHoleShape(),
                                    round(_hole_gap(fab,other)*1e6)):
                    raise ValueError('current-sized array violates hole spacing')
    # All geometric prechecks precede mutation, including replacement removal.
    if any(t.GetClass()!='PAD' and t.IsLocked() for t in group):
        raise ValueError('locked group copper')
    for t in group:
        if t.GetClass()!='PAD':board.Remove(t)
    for item in planned:
        board.Add(item);item.thisown=False
    board.BuildConnectivity()
    return dict(
        sizing,
        previous_vias=len(vias),
        bus_width_mm=bus_width,
        feed_width_mm=feed_width,
        span_mm=span,
        ref=intent["ref"],
        address=intent["address"],
    )


def main():
    import argparse, json, shutil
    from pathlib import Path
    from types import SimpleNamespace
    import pcbnew
    from pnr.plane_intent import read_annotations, resolve

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--annotation-source", action="append", type=Path, required=True)
    ap.add_argument("--fab-model", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    a = ap.parse_args()
    b = pcbnew.LoadBoard(str(a.board))
    b.BuildConnectivity()
    cs = [
        SimpleNamespace(
            ref=f.GetReference(),
            address=next(
                (
                    z.GetText()
                    for z in f.GetFields()
                    if z.GetName() == "atopile_address"
                ),
                "",
            ),
            pads=[
                SimpleNamespace(name=p.GetNumber(), net=p.GetNetname())
                for p in f.Pads()
            ],
        )
        for f in b.GetFootprints()
    ]
    intents = resolve(read_annotations(a.annotation_source), cs)
    from pnr.fab_profile import apply_fab_model
    fab = apply_fab_model(json.loads(a.fab_model.read_text()))
    results = []
    for intent in intents:
        if intent["kind"] != "power_array":
            raise ValueError(
                "unsupported generation kind; do not silently discard source intent"
            )
        results.append(dict(intent=intent, result=replace_power_array(b, intent, fab)))
    a.report.write_text(
        json.dumps(dict(intents=results, fab_model=fab), indent=2) + "\n"
    )
    # Fill only after reloading this persisted transaction in the planes stage.
    # KiCad 10's filler was unstable when called in the mutation helper lifetime.
    pcbnew.SaveBoard(str(a.out), b)
    if a.out != a.board and a.board.with_suffix(".kicad_pro").exists():
        shutil.copyfile(
            a.board.with_suffix(".kicad_pro"), a.out.with_suffix(".kicad_pro")
        )


if __name__ == "__main__":
    main()
