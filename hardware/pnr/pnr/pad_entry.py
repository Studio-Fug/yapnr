"""Conservative minimum-width pad-entry witnesses and local branch completion.

A native shape intersection is insufficient. Require the source-sized trace width. At a smaller package land, require
full land-width entry; larger lands retain the required-width contact disk.
This is a local entry check, not an end-to-end thermal/ampacity proof. Unsupported
shapes, undersized pads/traces, and blocked repairs remain explicit findings.
"""

import math


def closest(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d)) if d else 0.0
    return (a[0] + t * dx, a[1] + t * dy)


def disk_in_roundrect(point, center, size, angle, corner, diameter):
    """Analytic rounded-rectangle signed distance, dimensions in mm."""
    co, si = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    x, y = point[0] - center[0], point[1] - center[1]
    x, y = abs(co * x + si * y), abs(-si * x + co * y)
    qx, qy = x - size[0] / 2 + corner, y - size[1] / 2 + corner
    sdf = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - corner
    return sdf <= -diameter / 2 + 1e-6


def xy(p):
    return (p.x / 1e6, p.y / 1e6)


def required_width(pad, rules):
    # widths are resolved upstream by constraints.py from source current inputs.
    width = rules.get("fab", {}).get("track_width_mm", 0.2)
    for cls in rules.get("net_classes", []):
        if pad.GetNetname() in cls.get("nets", []) and cls.get("width_mm") is not None:
            width = max(width, cls["width_mm"])
    width = max(width, rules.get("electrical_nets", {}).get(pad.GetNetname(), {}).get("outer_width_mm", 0))
    if rules.get('electrical_fab'):
        from pnr.electrical import terminal_policy
        p = terminal_policy(pad.GetParentFootprint().GetReference(), [pad.GetNumber()], pad.GetNetname(), rules)
        if p: width = p['outer_width_mm']
    contract = width_contract(pad, rules)
    if contract:  # PNR_TERMINAL_MIN_WIDTH=1: source minimum is a hard floor
        width = max(width, contract['min_width_mm'])
    return width


def width_contract(pad, rules):
    """The pad's @pnr-terminal-width contract (PNR_TERMINAL_MIN_WIDTH=1) or None."""
    if not rules.get('terminal_width_intents'):
        return None
    from pnr.electrical import terminal_width
    return terminal_width(pad.GetParentFootprint().GetReference(), [pad.GetNumber()], pad.GetNetname(), rules)


def entry_widths(pad, rules):
    """Widths to try for a new entry at this pad, widest first.

    ``[preferred, required]`` when a terminal width contract prefers more than the
    hard requirement; otherwise ``[required]`` (the pre-contract behaviour). A
    fallback never goes below ``required_width``.
    """
    required = required_width(pad, rules)
    contract = width_contract(pad, rules)
    if contract and contract['preferred_width_mm'] > required + 1e-9:
        return [contract['preferred_width_mm'], required]
    return [required]


def width_choice(pad, rules, width_mm):
    """Label a chosen entry width: 'preferred', 'required' (fallback) or None
    (no contract on this pad)."""
    contract = width_contract(pad, rules)
    if not contract:
        return None
    return 'preferred' if width_mm + 1e-6 >= contract['preferred_width_mm'] else 'required'


def widen_contract_stub(board, pad, layer, touching, rules, oracle, max_segments=4, max_length_mm=3.0):
    """Widen an existing narrow terminal stub in place (PNR_TERMINAL_MIN_WIDTH=1).

    Copper reused from a block macro or an earlier pass can predate the contract.
    Follow the narrow same-net chain from the pad centre to its via, a plated
    hole or copper already at the required width (at most ``max_segments`` /
    ``max_length_mm``, no branches) and overlay it at the preferred width, else
    the required width, each segment checked by the native clearance ``oracle``.
    Existing copper is never deleted or narrowed. Returns a record or None.
    """
    import pcbnew
    if pad.GetShape() not in (pcbnew.PAD_SHAPE_RECT, pcbnew.PAD_SHAPE_ROUNDRECT,
                              pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_CIRCLE):
        return None
    required = required_width(pad, rules)
    narrow = [t for t in touching if t.GetWidth() / 1e6 + 1e-6 < required]
    if not narrow:
        return None
    code, net, center = pad.GetNetCode(), pad.GetNetname(), xy(pad.GetPosition())
    same = [t for t in board.GetTracks() if t.GetNetCode() == code]
    tracks = [t for t in same if t.GetClass() == 'PCB_TRACK' and t.GetLayer() == layer]
    holes = [xy(t.GetPosition()) for t in same if t.GetClass() == 'PCB_VIA']
    holes += [xy(q.GetPosition()) for f in board.GetFootprints() for q in f.Pads()
              if q.GetNetCode() == code and q.GetAttribute() == pcbnew.PAD_ATTRIB_PTH]
    near = lambda a, b: math.dist(a, b) <= 1e-3
    ends = lambda t: (xy(t.GetStart()), xy(t.GetEnd()))
    for first in sorted(narrow, key=lambda t: min(math.dist(center, e) for e in ends(t))):
        a, z = ends(first)
        far = z if math.dist(center, z) >= math.dist(center, a) else a
        path, seen, done = [center, far], {first.m_Uuid.AsString()}, False
        for _ in range(max_segments):
            if any(near(far, h) for h in holes):
                done = True
                break
            joined = [t for t in tracks if t.m_Uuid.AsString() not in seen and any(near(far, e) for e in ends(t))]
            if len(joined) != 1:
                break  # dead end or branch: not a simple terminal stub
            nxt = joined[0]
            if nxt.GetWidth() / 1e6 + 1e-6 >= required:
                done = True  # reached copper already at the required width
                break
            seen.add(nxt.m_Uuid.AsString())
            far = next(e for e in ends(nxt) if not near(far, e)) if not near(*ends(nxt)) else far
            path.append(far)
            if sum(math.dist(p, q) for p, q in zip(path, path[1:])) > max_length_mm:
                break
        if not done or sum(math.dist(p, q) for p, q in zip(path, path[1:])) > max_length_mm:
            continue
        path = [p for i, p in enumerate(path) if i == 0 or not near(p, path[i - 1])]
        if len(path) < 2:
            continue
        for width in entry_widths(pad, rules):
            if not all(oracle.clear(net, layer, p, q, width) for p, q in zip(path, path[1:])):
                continue
            new = []
            for p, q in zip(path, path[1:]):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(round(p[0] * 1e6), round(p[1] * 1e6)))
                t.SetEnd(pcbnew.VECTOR2I(round(q[0] * 1e6), round(q[1] * 1e6)))
                t.SetWidth(round(width * 1e6))
                t.SetLayer(layer)
                t.SetNetCode(code)
                new.append(t)
            if not witness(pad, new[0], required):
                continue
            for t, (p, q) in zip(new, zip(path, path[1:])):
                board.Add(t)
                t.thisown = False
                oracle.reserve_track(net, layer, p, q, width)
            return dict(width_mm=width, choice=width_choice(pad, rules, width), segments=len(new),
                        length_mm=round(sum(math.dist(p, q) for p, q in zip(path, path[1:])), 4))
    return None


def terminal_width_report(board, rules):
    """Post-route classification of every contracted terminal pad.

    Per pad and copper layer, using the same full-entry :func:`witness` as
    :func:`inspect`: ``preferred`` (a touching track witnesses an entry at the
    preferred width), ``required`` (only at the hard minimum: clearance fallback),
    ``below_min`` (every touching track is narrower than the minimum),
    ``unqualified`` (wide enough but no full entry, e.g. grazing), ``in_pad``
    (qualified in-pad via array, no surface trace) or ``no_track``.
    ``entry_width_mm`` is the widest track witnessing the minimum entry. The
    hard-floor failures also appear as blocked entries. Empty when the flag is off.
    """
    import pcbnew
    rows = []
    if not rules.get('terminal_width_intents'):
        return rows
    tracks = [t for t in board.GetTracks() if t.GetClass() == 'PCB_TRACK']
    attached = None
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            contract = width_contract(pad, rules) if pad.GetNetCode() else None
            if not contract:
                continue
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                if not pad.IsOnLayer(layer):
                    continue
                shape = pad.GetEffectiveShape(layer)
                touching = [t for t in tracks if t.GetNetCode() == pad.GetNetCode() and t.GetLayer() == layer
                            and shape.Collide(t.GetEffectiveShape(layer), 0)]
                lo, hi = contract['min_width_mm'], contract['preferred_width_mm']
                qualified = [t.GetWidth() / 1e6 for t in touching if witness(pad, t, lo)]
                width = max(qualified) if qualified else None
                if any(witness(pad, t, hi) for t in touching):
                    status = 'preferred'
                elif qualified:
                    status = 'required'
                elif touching and all(t.GetWidth() / 1e6 + 1e-6 < lo for t in touching):
                    status = 'below_min'
                elif touching:
                    status = 'unqualified'
                else:
                    if attached is None:
                        attached = array_attached_pads(board, rules)
                    status = 'in_pad' if pad.m_Uuid.AsString() in attached else 'no_track'
                rows.append(dict(pad=fp.GetReference() + '.' + pad.GetNumber(), net=pad.GetNetname(),
                                 layer=board.GetLayerName(layer), status=status, entry_width_mm=width,
                                 min_width_mm=contract['min_width_mm'],
                                 preferred_width_mm=contract['preferred_width_mm']))
    return rows


def rectangular_custom_land(pad,layer):
    """Recognize actual rectangular custom copper, never its construction anchor."""
    import pcbnew as k
    if pad.GetShape()!=k.PAD_SHAPE_CUSTOM:return None
    poly=k.SHAPE_POLY_SET();pad.TransformShapeToPolygon(poly,layer,0,1000,k.ERROR_INSIDE)
    if poly.OutlineCount()!=1 or poly.HoleCount(0) or poly.COutline(0).PointCount()!=4:return None
    outline=poly.COutline(0);box=outline.BBox();points={(outline.CPoint(i).x,outline.CPoint(i).y) for i in range(4)}
    if points!={(x,y) for x in (box.GetLeft(),box.GetRight()) for y in (box.GetTop(),box.GetBottom())}:return None
    return xy(box.GetCenter()),(box.GetWidth()/1e6,box.GetHeight()/1e6)


def witness(pad, track, width):
    import pcbnew

    if track.GetClass() != "PCB_TRACK" or track.GetWidth() / 1e6 + 1e-6 < width:
        return False
    rectangle=rectangular_custom_land(pad,track.GetLayer())
    if rectangle:
        center,size=rectangle;contact=min(width,min(size));q=closest(center,xy(track.GetStart()),xy(track.GetEnd()))
        distance=math.dist(q,center);shift=min(distance,max(0.,(track.GetWidth()/1e6-contact)/2))
        if distance:q=tuple(q[i]+(center[i]-q[i])*shift/distance for i in (0,1))
        return disk_in_roundrect(q,center,size,0,0,contact)
    if pad.GetShape() == pcbnew.PAD_SHAPE_CUSTOM:
        # Custom copper has a tiny construction anchor; its nominal GetSize is
        # not the conductive pad. Require the same full-width disk in its actual
        # polygon, conservatively eroded with a micron-scale geometry margin.
        polygon = pcbnew.SHAPE_POLY_SET()
        pad.TransformShapeToPolygon(polygon, track.GetLayer(), 0, 1000, pcbnew.ERROR_INSIDE)
        polygon.Inflate(-round(width*500000)-2000,
                        pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, 1000)
        if not polygon.OutlineCount():
            return False
        center_region = pcbnew.SHAPE_SEGMENT(track.GetStart(), track.GetEnd(),
            max(0, track.GetWidth()-round(width*1e6)))
        return polygon.Collide(center_region, 0)
    size = xy(pad.GetSize())
    shape = pad.GetShape()
    if shape == pcbnew.PAD_SHAPE_RECT:
        radius = 0.0
    elif shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        radius = pad.GetRoundRectCornerRadius() / 1e6
    elif shape in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_OVAL):
        radius = min(size) / 2
    else:
        return False
    q = closest(xy(pad.GetPosition()), xy(track.GetStart()), xy(track.GetEnd()))
    # A wide bus may cover the pad even when its centerline runs outside it.
    center = xy(pad.GetPosition())
    distance = math.dist(q, center)
    contact = min(width, min(size))
    shift = min(distance, max(0.0, (track.GetWidth() / 1e6 - contact) / 2))
    if distance:
        q = (
            q[0] + (center[0] - q[0]) * shift / distance,
            q[1] + (center[1] - q[1]) * shift / distance,
        )
    # A full-current-width trace may widen a smaller package land. It still
    # needs a full land-width entry at its center; this never permits a narrower
    # trace (the required trace-width gate above is unchanged).
    # KiCad positive angles rotate toward negative native Y; the analytic
    # disk predicate uses Cartesian angles in these native XY coordinates.
    return disk_in_roundrect(
        q, xy(pad.GetPosition()), size, -pad.GetOrientation().AsDegrees(), radius, min(width,min(size))
    )



def neck_witness(pad, track, width, tracks, rules):
    """A source-authorized short neck must end in a full-current-width feed."""
    if not rules.get('electrical_fab') or track.GetClass()!='PCB_TRACK':return False
    from pnr.electrical import terminal_policy,neck_budget
    policy=terminal_policy(pad.GetParentFootprint().GetReference(),[pad.GetNumber()],pad.GetNetname(),rules)
    if not policy:return False
    narrow=track.GetWidth()/1e6
    if narrow>=width or narrow<rules.get('fab',{}).get('track_width_mm',.2):return False
    if not neck_budget(policy,narrow,track.GetLength()/1e6,rules['electrical_fab']):return False
    if not witness(pad,track,narrow):return False
    center=xy(pad.GetPosition());a,z=xy(track.GetStart()),xy(track.GetEnd())
    import pcbnew
    if pad.GetShape() in (pcbnew.PAD_SHAPE_RECT,pcbnew.PAD_SHAPE_ROUNDRECT,pcbnew.PAD_SHAPE_OVAL,pcbnew.PAD_SHAPE_CIRCLE) or rectangular_custom_land(pad,track.GetLayer()):
        probe=pcbnew.PCB_TRACK(pad.GetBoard());probe.SetLayer(track.GetLayer());probe.SetWidth(track.GetWidth())
        qualified=[]
        for endpoint in (track.GetStart(),track.GetEnd()):
            probe.SetStart(endpoint);probe.SetEnd(endpoint)
            qualified.append(witness(pad,probe,narrow))
        if qualified[0]==qualified[1]:return False
        far=track.GetEnd() if qualified[0] else track.GetStart()
    else:
        if min(math.dist(center,a),math.dist(center,z))>1e-6:return False
        far=track.GetEnd() if math.dist(center,a)<math.dist(center,z) else track.GetStart()
    import pcbnew
    return any(t!=track and t.GetClass()=='PCB_TRACK' and t.GetNetCode()==track.GetNetCode() and t.GetLayer()==track.GetLayer() and t.GetWidth()/1e6+1e-6>=width and math.dist(xy(far), closest(xy(far),xy(t.GetStart()),xy(t.GetEnd()))) + narrow/2 <= t.GetWidth()/2e6 + 1e-6 for t in tracks)

def connected_land_witness(target,anchor,layer,rules):
    """Carry qualification across a full-width overlap with a custom pad bus.

    Require same package/net, explicit common current scope (or the same pin),
    a previously qualified source entry, and a full conventional-land-width disk in
    actual overlap copper. The full group current remains required at both
    ends. Only recognized rectangular custom buses and conventional lands are
    supported. A gap or grazing overlap cannot carry qualification.
    """
    import pcbnew as k
    if target.GetParentFootprint()!=anchor.GetParentFootprint() or target.GetNetCode()!=anchor.GetNetCode():return False
    target_rectangle=rectangular_custom_land(target,layer)
    anchor_rectangle=rectangular_custom_land(anchor,layer)
    if bool(target_rectangle)==bool(anchor_rectangle):return False
    supported=(k.PAD_SHAPE_RECT,k.PAD_SHAPE_ROUNDRECT,k.PAD_SHAPE_OVAL,k.PAD_SHAPE_CIRCLE)
    if not target_rectangle and target.GetShape() not in supported:return False
    if not anchor_rectangle and anchor.GetShape() not in supported:return False
    if required_width(anchor,rules)+1e-6<required_width(target,rules):return False
    ref=target.GetParentFootprint().GetReference()
    if target.GetNumber()!=anchor.GetNumber() and not any(a.get('scope')=='terminal' and a['ref']==ref and a['net']==target.GetNetname() and {target.GetNumber(),anchor.GetNumber()}<=set(a['pads']) for a in rules.get('current_intents',[])):return False
    # The conventional land already carries its full source current when its
    # full land-width entry qualifies. The same full land-width disk must exist
    # in the pad-to-bus overlap; this is not a trace-width/current reduction.
    target_size=target_rectangle[1] if target_rectangle else xy(target.GetSize())
    anchor_size=anchor_rectangle[1] if anchor_rectangle else xy(anchor.GetSize())
    # Preserve the original bus-to-land criterion. A small bus must never
    # lower the required entry disk on a larger conventional land. In reverse,
    # require the entire conventional source-land contact in the overlap.
    conventional_size=anchor_size if target_rectangle else target_size
    contact=min(required_width(target,rules),min(conventional_size))
    if contact<=.004:return False
    overlap=k.SHAPE_POLY_SET();target.TransformShapeToPolygon(overlap,layer,0,1000,k.ERROR_INSIDE)
    bus=k.SHAPE_POLY_SET();anchor.TransformShapeToPolygon(bus,layer,0,1000,k.ERROR_INSIDE);overlap.BooleanIntersection(bus)
    # Up to2um tolerance for the native polygon approximation, same as existing
    # pad-land witnesses; a tiny corner overlap cannot contain this disk.
    overlap.Inflate(-max(1,round(contact*500000)-1000),k.CORNER_STRATEGY_ROUND_ALL_CORNERS,1000)
    return not overlap.IsEmpty()


def array_attached_pads(board, rules, tracks=None):
    """UUIDs of SMD pads whose terminal is attached by a filled in-pad via array
    (pnr.via_in_pad.array_attach: qualified 5B vias inside the pad, connected to one
    full-width trunk on another layer, combined barrel capacity >= the pad's
    budget). Empty without a via-in-pad profile policy (legacy) or electrical
    policy, so pre-profile entries are unchanged. Barrels alone never qualify."""
    if not rules.get('electrical_fab'):
        return set()
    from pnr.fab_profile import geometry
    g = geometry(rules)
    if g.in_pad is None:
        return set()
    import pcbnew
    from pnr.via_in_pad import array_attach
    vias = {}
    for t in (board.GetTracks() if tracks is None else tracks):
        if t.GetClass() == 'PCB_VIA':
            vias.setdefault(t.GetNetCode(), []).append(t.GetPosition())
    out = set()
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not pad.GetNetCode() or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            box = pad.GetBoundingBox()
            if not any(box.Contains(p) for p in vias.get(pad.GetNetCode(), ())):
                continue
            report = array_attach(board, pad, rules, g)
            if report and report['qualified']:
                out.add(pad.m_Uuid.AsString())
    return out


def inspect(board, rules):
    import pcbnew

    tracks = list(board.GetTracks())
    from collections import defaultdict
    by_net_layer=defaultdict(list)
    for t in tracks:
        if t.GetClass()=='PCB_TRACK':by_net_layer[t.GetNetCode(),t.GetLayer()].append(t)
    # Profile 5B only: a terminal attached by its in-pad array (no surface trace
    # needed on its own layer) is a qualified entry; see array_attached_pads.
    attached = array_attached_pads(board, rules, tracks)
    records = []; record_groups=[]; groups=defaultdict(set)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not pad.GetNetCode() or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
                continue
            for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
                if not pad.IsOnLayer(layer):
                    continue
                shape=pad.GetEffectiveShape(layer)
                touching = [
                    t
                    for t in by_net_layer[pad.GetNetCode(),layer]
                    if shape.Collide(
                        t.GetEffectiveShape(layer), 0
                    )
                ]
                width = required_width(pad, rules)
                group=(fp.m_Uuid.AsString(),pad.GetNetCode(),layer)
                groups[group].add(len(records));record_groups.append(group)
                records.append(
                    (
                        pad,
                        layer,
                        touching,
                        width,
                        any(witness(pad, t, width) or neck_witness(pad,t,width,tracks,rules) for t in touching)
                        or pad.m_Uuid.AsString() in attached,
                    )
                )
    # Propagate only from an independently qualified trace/neck entry, through
    # explicit full-width pad overlaps. A cycle of bare/grazing lands cannot
    # bootstrap its own qualification. Include untracked bus pads as bridges,
    # but keep the public report scoped to actual track contacts.
    good={i for i,row in enumerate(records) if row[4]}
    while True:
        added={i for i,(p,layer,_,_,_) in enumerate(records) if i not in good and
               any(records[j][1]==layer and connected_land_witness(p,records[j][0],layer,rules) for j in groups[record_groups[i]] & good)}
        if not added:break
        good.update(added)
    # An in-pad array attach is reported even without a surface trace on the land.
    return [(p,layer,touching,width,i in good) for i,(p,layer,touching,width,_) in enumerate(records)
            if touching or p.m_Uuid.AsString() in attached]


def snapshot(board, rules):
    return {
        p.m_Uuid.AsString() + ":" + str(la): good
        for p, la, ts, w, good in inspect(board, rules)
    }


def repair(board, rules, only_keys=None):
    """Add short center-to-trace branches; never delete or narrow existing copper.

    Foreign copper and rule-area broad bounds gate proposals. Native DRC remains
    mandatory after persisted reload/refill, including holes and board edges.
    """
    import pcbnew

    added, blocked = [], []
    oracle = None  # native clearance oracle, built only for contracted terminals
    for p, layer, touching, width, good in inspect(board, rules):
        if good or (only_keys is not None and p.m_Uuid.AsString()+":"+str(layer) not in only_keys):
            continue
        label = p.GetParentFootprint().GetReference() + "." + p.GetNumber()
        reason = "blocked or unsupported entry"
        start = xy(p.GetPosition())
        if p.GetShape() == pcbnew.PAD_SHAPE_CUSTOM:
            interior = pcbnew.SHAPE_POLY_SET()
            p.TransformShapeToPolygon(interior, layer, 0, 1000, pcbnew.ERROR_INSIDE)
            interior.Inflate(-round(width*500000)-2000,
                             pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, 1000)
            points = [xy(interior.COutline(i).CPoint(j)) for i in range(interior.OutlineCount())
                      for j in range(interior.COutline(i).PointCount())]
            if not points:
                blocked.append(dict(pad=label,net=p.GetNetname(),required_width_mm=width,
                                    reason='custom copper cannot contain required-width entry'))
                continue
            start = min(points, key=lambda q:min(math.dist(q, closest(q,xy(t.GetStart()),xy(t.GetEnd()))) for t in touching))
        choices = sorted(
            touching,
            key=lambda t: math.dist(
                start, closest(start, xy(t.GetStart()), xy(t.GetEnd()))
            ),
        )
        # PNR_TERMINAL_MIN_WIDTH=1: a contracted terminal tries its preferred branch
        # width first, then the hard required width (entry_widths); every other pad
        # tries only ``width``, exactly as before.
        widths = entry_widths(p, rules) if width_contract(p, rules) else [width]
        for old in choices:
            if old.GetWidth() / 1e6 + 1e-6 < width:
                reason = "required width exceeds existing trace; needs current/neck policy"
                continue
            end = closest(start, xy(old.GetStart()), xy(old.GetEnd()))
            # A large conventional land may need a centre branch longer than
            # 1 mm even when the trace already touches its edge. Bound the
            # allowance by the land radius plus the existing 1 mm local reach;
            # never lower width or bypass foreign-copper/entry/native guards.
            local_reach = 1.0
            if p.GetShape() in (pcbnew.PAD_SHAPE_RECT, pcbnew.PAD_SHAPE_ROUNDRECT,
                                pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_CIRCLE):
                local_reach += math.hypot(*xy(p.GetSize())) / 2
            if math.dist(start, end) < 1e-6 or math.dist(start, end) > local_reach:
                continue
            placed = None
            for branch_width in widths:
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(round(start[0]*1e6),round(start[1]*1e6)))
                t.SetEnd(pcbnew.VECTOR2I(round(end[0] * 1e6), round(end[1] * 1e6)))
                t.SetWidth(round(branch_width * 1e6))
                t.SetLayer(layer)
                t.SetNetCode(p.GetNetCode())
                shape = t.GetEffectiveShape(layer)
                clr = round(rules.get("fab", {}).get("clearance_mm", 0.15) * 1e6)
                foreign = [x for f in board.GetFootprints() for x in f.Pads()] + list(
                    board.GetTracks()
                )
                if any(
                    x.IsOnLayer(layer)
                    and x.GetNetCode() != p.GetNetCode()
                    and shape.Collide(x.GetEffectiveShape(layer), clr)
                    for x in foreign
                ):
                    continue
                zones = list(board.Zones()) + [
                    z for f in board.GetFootprints() for z in f.Zones()
                ]
                if any(
                    z.GetIsRuleArea()
                    and z.IsOnLayer(layer)
                    and z.GetDoNotAllowTracks()
                    and z.GetBoundingBox().Intersects(t.GetBoundingBox())
                    for z in zones
                ):
                    continue
                if not witness(p, t, width):
                    continue
                placed = (t, branch_width)
                break
            if placed is None:
                continue
            t, branch_width = placed
            board.Add(t)
            t.thisown = False
            record = dict(
                pad=label, net=p.GetNetname(), width_mm=branch_width, start=start, end=end
            )
            if len(widths) > 1 or width_contract(p, rules):
                record['choice'] = width_choice(p, rules, branch_width)
            added.append(record)
            break
        else:
            if width_contract(p, rules):
                if oracle is None:
                    from pnr.native_electrical import Oracle
                    oracle = Oracle(board, rules)
                widened = widen_contract_stub(board, p, layer, touching, rules, oracle)
                if widened:
                    added.append(dict(pad=label, net=p.GetNetname(), widened=widened))
                    continue
            from pnr.pad_entry_neck import repair_neck
            neck = repair_neck(board, p, layer, touching, width, rules)
            if neck:
                added.append(dict(pad=label, net=p.GetNetname(), source_neck=neck))
                continue
            blocked.append(
                dict(
                    pad=label,
                    net=p.GetNetname(),
                    required_width_mm=width,
                    reason=reason,
                )
            )
    return dict(added=added, blocked=blocked)


def repair_changed_entries(board, rules, before):
    """Repair newly bad contacts and report lost witnesses for a transaction."""
    repairs = dict(added=[], blocked=[], passes=0)
    for _ in range(5):
        proposed = snapshot(board, rules)
        needs_entry = {key for key, good in proposed.items() if not good
                       and (key not in before or before[key])}
        if not needs_entry:
            repairs['blocked'] = []
            break
        step = repair(board, rules, only_keys=needs_entry)
        repairs['added'].extend(step['added'])
        repairs['blocked'] = step['blocked']
        repairs['passes'] += 1
        board.BuildConnectivity()
        if not step['added']:
            break
    current = snapshot(board, rules)
    return dict(entry_repairs=repairs,
                lost_pad_entries=[key for key, good in before.items()
                                  if good and not current.get(key, False)],
                new_bad_entries=[key for key, good in current.items()
                                 if not good and key not in before])


def main():
    import argparse, json, shutil
    from pathlib import Path
    import pcbnew

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    rules = json.loads(a.rules.read_text())
    b = pcbnew.LoadBoard(str(a.board))
    result = repair(b, rules)
    from pnr.electrical import terminal_min_width_enabled
    if terminal_min_width_enabled():
        # Side field (evaluation.json embeds this report): which contracted
        # terminals got the preferred width and which fell back to the minimum.
        rows = terminal_width_report(b, rules)
        counts = {}
        for row in rows:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        result['terminal_widths'] = dict(counts=counts, pads=rows)
    a.report.write_text(json.dumps(result, indent=2) + "\n")
    pcbnew.SaveBoard(str(a.out), b)
    if a.out != a.board:
        shutil.copyfile(
            a.board.with_suffix(".kicad_pro"), a.out.with_suffix(".kicad_pro")
        )
    if a.strict and result["blocked"]:
        raise SystemExit(
            "Unresolved pad-entry findings: " + str(len(result["blocked"]))
        )


if __name__ == "__main__":
    main()
