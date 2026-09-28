"""Native, transactional power/plane and coupled-pair routing adapters.

Add-only power repair preserves current-carrying trunks/arrays. A layer change
uses a complete source-sized via bank, not a single signal via. Pair replacement
is atomic for both nets; all old pad connectivity must survive. This module is
run by native_loop under KiCad Python, never in the torch interpreter.

Under a fab profile with a filled via-in-pad policy (jlc-pofv, 5B) a terminal
land without any budgeted surface attach may instead be attached by a
current-sized in-pad via array whose trunk continues on another layer
(:func:`power_plan`, pnr.via_in_pad.array_attach); legacy rules never do.
"""
import argparse
from collections import Counter,defaultdict
import json,math,shutil,subprocess,time,os
from pathlib import Path
from types import SimpleNamespace
from pnr.electrical import net_policy,current_width,terminal_policy,neck_budget
from pnr.plane_intent import size_array
from pnr.route.detail.keyhole import route,length,elbows
from pnr.route.detail.coupled import solve_pair,path_metrics
from pnr.pad_entry import snapshot,witness
from pnr.via_coalesce import partition,preserved,acceptable
from pnr.via_in_pad import smd_keepout_violated,hole_keepouts


def xy(p):return (p.x/1e6,p.y/1e6)
def uid(t):return t.m_Uuid.AsString()
def vec(p):
    import pcbnew as k
    return k.VECTOR2I(round(p[0]*1e6),round(p[1]*1e6))


class Oracle:
    """Actual native copper shapes, filled zones, rule areas, drills and edges."""
    def __init__(self,b,rules,ignored=(),deadline=math.inf):
        import pcbnew as k
        self.b=b;self.rules=rules;self.deadline=deadline;self.hits=Counter();self.via_hits=Counter();self.cache={}
        self.items=[p for f in b.GetFootprints() for p in f.Pads()]+list(b.GetTracks())
        self.layers=list(b.GetEnabledLayers().CuStack());self.obstacles=[];self.buckets=defaultdict(set)
        from pnr.writeback import outline_bounds
        from pnr.fab_profile import geometry
        # Fab numbers from the rules; per-hole-kind values are None (unused) for
        # legacy rules, which keep the original single hole clearance exactly.
        self.geometry=g=geometry(rules)
        npth_gap=.201 if g.npth_hole_clearance is None else g.npth_hole_clearance+.001
        self.box=outline_bounds(b);self.ignored=set(ignored);self.pair_reserved={}
        for t in self.items:
            if uid(t) in self.ignored:continue
            gap=net_policy(t.GetNetname(),rules)['clearance_mm']+.001
            for la in self.layers:
                if t.IsOnLayer(la):self.add(t.GetEffectiveShape(la),t.GetBoundingBox(),gap,t.GetNetname(),uid(t),la)
                if t.GetClass()=='PAD' and t.GetAttribute()==k.PAD_ATTRIB_NPTH:
                    self.add(t.GetEffectiveHoleShape(),t.GetBoundingBox(),npth_gap,None,uid(t),la)
                elif (g.pth_hole_clearance is not None and t.GetClass()=='PAD' and t.IsOnLayer(la)
                      and t.GetAttribute()==k.PAD_ATTRIB_PTH and max(t.GetDrillSize().x,t.GetDrillSize().y)>0):
                    # Plated pad hole to foreign copper (same net may enter its own land):
                    # component PTH 0.35; a footprint's via-class hole (drill < 0.30) 0.20.
                    kind=g.hole_kind(False,True,max(t.GetDrillSize().x,t.GetDrillSize().y)/1e6)
                    self.add(t.GetEffectiveHoleShape(),t.GetBoundingBox(),g.pad_hole_clearance(kind)+.001,t.GetNetname(),uid(t),la)
            # A via with removed unused pads (5B in-pad) is its bare hole where it has
            # no pad: foreign copper keeps the via hole clearance (0.20) from the wall.
            for la,hole,hole_gap in hole_keepouts(g,t,self.layers):self.add(hole,t.GetBoundingBox(),hole_gap,t.GetNetname(),uid(t),la)
        for z in b.Zones():
            for la in self.layers:
                if not z.IsOnLayer(la):continue
                if z.GetIsRuleArea():
                    if z.GetDoNotAllowTracks():self.add(z.Outline(),z.GetBoundingBox(),.001,None,uid(z),la)
                else:self.add(z.GetFilledPolysList(la),z.GetBoundingBox(),net_policy(z.GetNetname(),rules)['clearance_mm']+.001,z.GetNetname(),uid(z),la)
        self.probe=k.PCB_TRACK(b)
        self.drilled=[t for t in self.items if t.GetClass()=='PCB_VIA' or t.GetClass()=='PAD' and max(t.GetDrillSize().x,t.GetDrillSize().y)>0]
        # Cache exact physical shapes in 1 mm buckets. Vias query every crossed
        # layer and drill neighborhood; foreign filled planes remain cuttable.
        self.physical=[];self.physical_buckets=defaultdict(set)
        self.holes=[];self.hole_gaps=[];self.hole_buckets=defaultdict(set);self.clearance_cap=.05
        for item in self.items:self.index_physical(item)
        from pnr.reference_guard import ReferenceGuard
        self.reference_guard=ReferenceGuard(b,rules)
    def fork(self, deadline=None):
        """Isolate provisional copper while retaining the caller's obstacles."""
        import copy,pcbnew as k
        clone=copy.copy(self)
        for name in ('items','obstacles','physical','holes','hole_gaps','drilled'):
            setattr(clone,name,list(getattr(self,name)))
        for name in ('buckets','physical_buckets','hole_buckets'):
            setattr(clone,name,defaultdict(set,{key:set(values) for key,values in getattr(self,name).items()}))
        clone.ignored=set(self.ignored);clone.cache=dict(self.cache);clone.pair_reserved=dict(self.pair_reserved)
        clone.hits=Counter();clone.via_hits=Counter();clone.probe=k.PCB_TRACK(self.b)
        if deadline is not None:clone.deadline=min(self.deadline,deadline)
        return clone

    def index_physical(self,item):
        import pcbnew as k
        if uid(item) in self.ignored:return
        net=item.GetNetname();gap=net_policy(net,self.rules)['clearance_mm']+.001
        self.clearance_cap=max(self.clearance_cap,gap)
        # SMD pads carry the pad itself: pnr.via_in_pad judges vias against them.
        smd=item if item.GetClass()=='PAD' and item.GetAttribute()==k.PAD_ATTRIB_SMD else None
        for layer in self.layers:
            if not item.IsOnLayer(layer):continue
            shape=item.GetEffectiveShape(layer);box=shape.BBox();i=len(self.physical)
            self.physical.append((shape,net,gap,smd,uid(item)))
            for x in range(math.floor(box.GetLeft()/1e6),math.floor(box.GetRight()/1e6)+1):
                for y in range(math.floor(box.GetTop()/1e6),math.floor(box.GetBottom()/1e6)+1):self.physical_buckets[layer,x,y].add(i)
        # Removed-pad via: its hole wall keeps the via hole clearance (hole_keepouts).
        for layer,hole,hole_gap in hole_keepouts(self.geometry,item,self.layers):
            box=hole.BBox();i=len(self.physical);self.clearance_cap=max(self.clearance_cap,hole_gap)
            self.physical.append((hole,net,hole_gap,None,uid(item)))
            for x in range(math.floor(box.GetLeft()/1e6),math.floor(box.GetRight()/1e6)+1):
                for y in range(math.floor(box.GetTop()/1e6),math.floor(box.GetBottom()/1e6)+1):self.physical_buckets[layer,x,y].add(i)
        if item.GetClass()=='PCB_VIA' or item.GetClass()=='PAD' and max(item.GetDrillSize().x,item.GetDrillSize().y)>0:
            shape=item.GetEffectiveHoleShape();box=shape.BBox();i=len(self.holes);self.holes.append(shape)
            # Drill gap a new via needs to this hole (profile: via/PTH/NPTH differ;
            # a footprint's via-class pad hole counts as a via).
            via=item.GetClass()=='PCB_VIA'
            kind=self.geometry.hole_kind(via,None if via else item.GetAttribute()!=k.PAD_ATTRIB_NPTH,
                                         0 if via else max(item.GetDrillSize().x,item.GetDrillSize().y)/1e6)
            self.hole_gaps.append(self.geometry.via_hole_gap(kind))
            for x in range(math.floor(box.GetLeft()/1e6),math.floor(box.GetRight()/1e6)+1):
                for y in range(math.floor(box.GetTop()/1e6),math.floor(box.GetBottom()/1e6)+1):self.hole_buckets[x,y].add(i)
    def add(self,shape,box,gap,net,identity,la):
        index=len(self.obstacles);self.obstacles.append((shape,gap,net,identity))
        for x in range(math.floor(box.GetLeft()/1e6)-1,math.floor(box.GetRight()/1e6)+2):
            for y in range(math.floor(box.GetTop()/1e6)-1,math.floor(box.GetBottom()/1e6)+2):self.buckets[la,x,y].add(index)
    def clear(self,net,la,a,z,width,ignore_nets=(),pair=None):
        if time.monotonic()>self.deadline:raise TimeoutError('electrical search time budget')
        pair_key=(pair["p"],pair["n"],pair["gap_mm"]) if pair else None
        key=(net,la,tuple(a),tuple(z),width,tuple(ignore_nets),pair_key)
        if key in self.cache:return self.cache[key]
        edge=width/2+self.rules.get('fab',{}).get('edge_clearance_mm',.2)+.001
        if any(not(self.box.GetLeft()/1e6+edge<=p[0]<=self.box.GetRight()/1e6-edge and self.box.GetTop()/1e6+edge<=p[1]<=self.box.GetBottom()/1e6-edge) for p in (a,z)):return False
        self.probe.SetLayer(la);self.probe.SetStart(vec(a));self.probe.SetEnd(vec(z));self.probe.SetWidth(round(width*1e6));shape=self.probe.GetEffectiveShape(la)
        r=width/2+max(.201,net_policy(net,self.rules)['clearance_mm']+.001);found=set()
        for x in range(math.floor(min(a[0],z[0])-r),math.floor(max(a[0],z[0])+r)+1):
            for y in range(math.floor(min(a[1],z[1])-r),math.floor(max(a[1],z[1])+r)+1):found.update(self.buckets[la,x,y])
        for i in found:
            other,gap,n,identity=self.obstacles[i]
            if n==net or n in ignore_nets:continue
            if pair and {net,n}=={pair['p'],pair['n']} and identity in self.pair_reserved:
                from pnr.route.detail.regional import segment_distance
                other_a,other_z,other_width=self.pair_reserved[identity]
                required=(round(width*1e6)/1e6+other_width)/2+max(pair['gap_mm'],net_policy(net,self.rules)['clearance_mm'],net_policy(n,self.rules)['clearance_mm'])
                actual=segment_distance(xy(self.probe.GetStart()),xy(self.probe.GetEnd()),other_a,other_z)
                if actual+1e-9<required:
                    self.hits[identity]+=1;self.cache[key]=False;return False
                continue
            if other.Collide(shape,round(max(gap,net_policy(net,self.rules)['clearance_mm']+.001 if n is not None else gap)*1e6)):
                self.hits[identity]+=1;self.cache[key]=False;return False
        self.cache[key]=True;return True
    def reserve_track(self,net,layer,a,z,width):
        """Make already planned pair copper an obstacle for later stages/legs."""
        import pcbnew as k
        track=k.PCB_TRACK(self.b);track.SetLayer(layer);track.SetStart(vec(a));track.SetEnd(vec(z));track.SetWidth(round(width*1e6));track.SetNetCode(self.b.FindNet(net).GetNetCode())
        self.items.append(track);self.index_physical(track)
        self.pair_reserved[uid(track)]=(xy(track.GetStart()),xy(track.GetEnd()),track.GetWidth()/1e6)
        self.add(track.GetEffectiveShape(layer),track.GetBoundingBox(),net_policy(net,self.rules)['clearance_mm']+.001,net,uid(track),layer)
        self.cache.clear()

    def reserve_via(self,net,point,diameter=.6,drill=.3):
        import pcbnew as k
        via=k.PCB_VIA(self.b);via.SetPosition(vec(point));via.SetFrontWidth(round(diameter*1e6));via.SetDrill(round(drill*1e6));via.SetViaType(k.VIATYPE_THROUGH);via.SetLayerPair(k.F_Cu,k.B_Cu);via.SetNetCode(self.b.FindNet(net).GetNetCode())
        self.items.append(via);self.drilled.append(via);self.index_physical(via)
        for layer in self.layers:self.add(via.GetEffectiveShape(layer),via.GetBoundingBox(),net_policy(net,self.rules)['clearance_mm']+.001,net,uid(via),layer)
        self.cache.clear()

    def via(self,net,p,diameter,drill):
        import pcbnew as k
        # Through vias may cut clearance voids in foreign planes, but cannot
        # collide with tracks/pads/keepouts. Plane contact is checked after fill.
        v=k.PCB_VIA(self.b);v.SetPosition(vec(p));v.SetFrontWidth(round(diameter*1e6));v.SetDrill(round(drill*1e6));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu)
        if not self.reference_guard.via_clear(net,p,diameter):return False
        hole=v.GetEffectiveHoleShape();hg=self.geometry.max_via_hole_gap+.001
        box=hole.BBox();near=set()
        for x in range(math.floor(box.GetLeft()/1e6-hg),math.floor(box.GetRight()/1e6+hg)+1):
            for y in range(math.floor(box.GetTop()/1e6-hg),math.floor(box.GetBottom()/1e6+hg)+1):near.update(self.hole_buckets[x,y])
        if any(self.holes[i].Collide(hole,round((self.hole_gaps[i]+.001)*1e6)) for i in near):return False
        gap=net_policy(net,self.rules)['clearance_mm']+.001;r=max(gap,self.clearance_cap,.05)
        for la in self.layers:
            near=set();shape=v.GetEffectiveShape(la);box=shape.BBox()
            for x in range(math.floor(box.GetLeft()/1e6-r),math.floor(box.GetRight()/1e6+r)+1):
                for y in range(math.floor(box.GetTop()/1e6-r),math.floor(box.GetBottom()/1e6+r)+1):near.update(self.physical_buckets[la,x,y])
            for i in sorted(near):
                other,other_net,other_gap,smd,identity=self.physical[i]
                # Legacy: no via within 0.05 mm of any SMD pad. Profile: 5A 0.127 from
                # every SMD pad unless a qualified 5B filled in-pad via of its own net.
                if smd is not None and smd_keepout_violated(self.geometry,smd,other,shape,net,p,diameter,drill,la):return False
                if other_net!=net and other.Collide(shape,round(max(gap,other_gap)*1e6)):
                    self.via_hits[identity]+=1;return False
        for z in self.b.Zones():
            if z.GetIsRuleArea() and z.GetDoNotAllowVias() and z.Outline().Collide(v.GetEffectiveShape(k.F_Cu),1000):return False
        g=self.geometry
        edge=diameter/2+.201 if g.hole_to_edge is None else g.via_edge_margin(diameter,drill)
        return self.box.GetLeft()/1e6+edge<=p[0]<=self.box.GetRight()/1e6-edge and self.box.GetTop()/1e6+edge<=p[1]<=self.box.GetBottom()/1e6-edge


def connected_items(b,seed):
    cn=b.GetConnectivity()
    return list({uid(t):t for t in [seed]+list(cn.GetConnectedItems(seed)) if t.GetNetCode()==seed.GetNetCode() and t.GetClass() in ('PAD','PCB_TRACK','PCB_VIA')}.values())


def access(items,layer,width,towards=(),point_clear=None):
    """Existing narrow signal fanouts cannot become anchors for a power trunk."""
    out=set()
    for t in items:
        if not t.IsOnLayer(layer):continue
        if t.GetClass()=='PAD':
            import pcbnew as k
            if t.GetShape()==k.PAD_SHAPE_CUSTOM:
                polygon=k.SHAPE_POLY_SET()
                t.TransformShapeToPolygon(polygon,layer,0,1000,k.ERROR_INSIDE)
                polygon.Inflate(-round(width*500000)-2000,
                                k.CORNER_STRATEGY_ROUND_ALL_CORNERS,1000)
                for index in range(polygon.OutlineCount()):
                    outline=polygon.COutline(index)
                    center=outline.BBox().GetCenter()
                    if polygon.Contains(center):out.add(xy(center))
                    out.update(xy(outline.CPoint(j)) for j in range(outline.PointCount()))
            # Full-width copper may land on a smaller pad. The actual proposed
            # width still clears all foreign copper, and pad-entry checks require
            # full land contact rather than a grazing overlap.
            elif min(xy(t.GetSize()))>0:
                center=xy(t.GetPosition());out.add(center)
                sx,sy=xy(t.GetSize());contact=min(width,sx,sy)
                if width>contact+1e-6 and (point_clear is None or not point_clear(center)) and t.GetShape() in (k.PAD_SHAPE_RECT,k.PAD_SHAPE_ROUNDRECT,k.PAD_SHAPE_OVAL,k.PAD_SHAPE_CIRCLE):
                    # Full-width round caps can cover the complete land contact
                    # disk without their centerline entering the pad. This is
                    # NOT a narrowed neck: width and all current budgets stay.
                    offset=max(sx,sy)/2-min(sx,sy)/2
                    angle=math.radians(-t.GetOrientation().AsDegrees())
                    vx,vy=(offset,0) if sx>=sy else (0,offset)
                    vx,vy=vx*math.cos(angle)-vy*math.sin(angle),vx*math.sin(angle)+vy*math.cos(angle)
                    origins=[center,(center[0]+vx,center[1]+vy),(center[0]-vx,center[1]-vy)]
                    reach=max(0.,(width-contact)/2-.002)
                    probe=k.PCB_TRACK(t.GetBoard());probe.SetLayer(layer);probe.SetWidth(round(width*1e6))
                    for origin in origins:
                        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(.7071067812,.7071067812),(.7071067812,-.7071067812),(-.7071067812,.7071067812),(-.7071067812,-.7071067812)):
                            pt=(origin[0]+dx*reach,origin[1]+dy*reach)
                            probe.SetStart(vec(pt));probe.SetEnd(vec(pt))
                            if witness(t,probe,width):out.add(pt)
        elif t.GetClass()=='PCB_TRACK' and t.GetWidth()/1e6+1e-6>=width:
            from pnr.pad_entry import closest
            a,z=xy(t.GetStart()),xy(t.GetEnd());out.update([a,z])
            out.update(closest(point,a,z) for point in towards)
        # A lone existing via is not a proven power-array port. Banks generated
        # here are handled as aggregate transitions, never inferred by proximity.
    return sorted(out)


def add_track(b,net,la,a,z,width):
    import pcbnew as k
    if math.dist(a,z)<1e-8:return None
    t=k.PCB_TRACK(b);t.SetNetCode(b.FindNet(net).GetNetCode());t.SetLayer(la);t.SetStart(vec(a));t.SetEnd(vec(z));t.SetWidth(round(width*1e6));b.Add(t);return t


def bank_points(center,count,diameter,drill,hole_clearance):
    pitch=max(diameter+.05,drill+hole_clearance+.002)
    cols=math.ceil(math.sqrt(count));rows=math.ceil(count/cols)
    return [(center[0]+(i%cols-(cols-1)/2)*pitch,center[1]+(i//cols-(rows-1)/2)*pitch) for i in range(count)]


def bank_clear(oracle,net,center,policy,layers):
    sizing=policy.get('via_array')
    if not sizing:return None
    fab=oracle.rules.get('fab',{})
    points=bank_points(center,sizing['count'],sizing['diameter_mm'],sizing['drill_mm'],fab.get('hole_to_hole_mm',fab.get('hole_clearance_mm',.2)))
    if not all(oracle.via(net,p,sizing['diameter_mm'],sizing['drill_mm']) for p in points):return None
    for la,width in layers:
        if not all(oracle.clear(net,la,center,p,width) for p in points):return None
    return points


def add_in_pad_vias(b,net,vias,geometry):
    """Filled 5B in-pad through vias ``[(point, diameter, drill)]`` with their unused
    inner pads removed (via_in_pad.style_in_pad_via, 5B "Inner layers")."""
    import pcbnew as k
    from pnr.via_in_pad import style_in_pad_via
    keep=[]
    for point,diameter,drill in vias:
        v=k.PCB_VIA(b);v.SetNetCode(b.FindNet(net).GetNetCode());v.SetPosition(vec(point));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu)
        v.SetFrontWidth(round(diameter*1e6));v.SetDrill(round(drill*1e6));style_in_pad_via(geometry,v);b.Add(v);keep.append(v)
    return keep


def add_bank(b,net,center,points,policy,layers):
    import pcbnew as k
    sizing=policy['via_array'];keep=[]
    for p in points:
        v=k.PCB_VIA(b);v.SetNetCode(b.FindNet(net).GetNetCode());v.SetPosition(vec(p));v.SetFrontWidth(round(sizing['diameter_mm']*1e6));v.SetDrill(round(sizing['drill_mm']*1e6));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu);b.Add(v);keep.append(v)
        for la,width in layers:keep.append(add_track(b,net,la,center,p,width))
    return keep


def qualified_tree_pads(b,net,layer,policy,rules,anchors,excluded=()):
    """Reuse annotated full-current lands only along a width-qualified surface path.

    Connectivity alone is insufficient: reject thin sense links, grazing copper
    junctions and unproven via capacity. Source-authorized necks retain their full
    loss/drop/length checks. No pad or track is widened or deleted here.
    """
    if not policy.get('current_known'):return []
    from pnr.pad_entry import neck_witness
    from pnr.route.detail.regional import segment_distance
    tracks=[t for t in b.GetTracks() if t.GetClass()=='PCB_TRACK' and t.GetNetname()==net and t.GetLayer()==layer]
    anchor_ids={uid(t) for t in anchors if t.GetLayer()==layer}
    if not anchor_ids:return []
    result=[]
    for fp in b.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname()!=net or not pad.IsOnLayer(layer) or uid(pad) in excluded:continue
            terminal=terminal_policy(fp.GetReference(),[pad.GetNumber()],net,rules)
            if not terminal or terminal['rms_current_a']<policy['rms_current_a'] or terminal['peak_current_a']<policy['peak_current_a']:continue
            width=terminal['outer_width_mm']
            good=[t for t in tracks if t.GetWidth()/1e6+1e-6>=width or neck_witness(pad,t,width,tracks,rules)]
            reached={uid(t) for t in good if witness(pad,t,min(width,t.GetWidth()/1e6))}
            todo=[t for t in good if uid(t) in reached]
            while todo and not reached&anchor_ids:
                first=todo.pop();fw=first.GetWidth()/1e6
                for other in good:
                    if uid(other) in reached:continue
                    ow=other.GetWidth()/1e6;contact=min(width,fw,ow)
                    distance=segment_distance(xy(first.GetStart()),xy(first.GetEnd()),xy(other.GetStart()),xy(other.GetEnd()))
                    if distance<=(fw+ow)/2-contact+1e-6:
                        reached.add(uid(other));todo.append(other)
            if reached&anchor_ids:result.append(pad)
    return result


def power_plan(b,net,source,target,rules,oracle,bounds,pitch,*,prefer_tree=True,root_strategy=None,_source_only=False,_reverse_retry=True):
    import pcbnew as k
    p=net_policy(net,rules);aa=connected_items(b,source);zz=connected_items(b,target)
    trunk=dict(p)
    if root_strategy is None:root_strategy='existing' if prefer_tree else 'target'
    for seed,group in (((source,aa),) if _source_only else ((source,aa),(target,zz))):
        ps=[t for t in group if t.GetClass()=='PAD']
        refs={t.GetParentFootprint().GetReference() for t in ps}
        if len(refs)!=1:continue
        leaf=terminal_policy(next(iter(refs)),[t.GetNumber() for t in ps],net,rules)
        if leaf:
            if seed==target:source,target=target,source;aa,zz=zz,aa
            p=leaf
            break
    # Local branch budgets may reduce a lead width, never use an existing thin
    # sense lead as an anchor for a new power trunk.
    source_ids={uid(item) for item in aa}
    trunk_anchors=[t for t in b.GetTracks() if t.GetNetname()==net and uid(t) not in source_ids and t.GetClass()=='PCB_TRACK']
    root_landings={}
    from functools import lru_cache
    access_cache={}
    def power_access(items,layer,width,towards=()):
        # A blocked round cap cannot seed any legal full-width segment. Remove
        # these endpoints once, before Cartesian elbow/grid searches. Geometry
        # stays immutable throughout this plan; the cache never crosses edits.
        items=list(items)
        key=(tuple(sorted(uid(t) for t in items)),layer,width,tuple(towards))
        if key not in access_cache:
            access_cache[key]=tuple(pt for pt in access(items,layer,width,towards,point_clear=lambda q:oracle.clear(net,layer,q,q,width))
                                    if oracle.clear(net,layer,pt,pt,width))
        return list(access_cache[key])
    # Same-net copper is not automatically a safe attachment: a thin branch
    # grazing an otherwise bare power pad creates an unqualified pad entry.
    from pnr.pad_entry import required_width,array_attached_pads
    existing_entries=snapshot(b,rules)
    guarded_pads=[(pad,required_width(pad,rules)) for f in b.GetFootprints() for pad in f.Pads()
                  if pad.GetNetname()==net and pad.GetAttribute()==k.PAD_ATTRIB_SMD]
    # Terminals already attached by a qualified in-pad array (profile 5B only;
    # always empty for legacy rules): their entry carries their full budget.
    attached_cache={}
    def array_attached():
        if 'ids' not in attached_cache:
            attached_cache['ids']=array_attached_pads(b,rules) if oracle.geometry.in_pad is not None else set()
        return attached_cache['ids']
    probe=k.PCB_TRACK(b)
    def entry_clear(net,layer,a,z,width):
        if not oracle.clear(net,layer,a,z,width):return False
        probe.SetLayer(layer);probe.SetStart(vec(a));probe.SetEnd(vec(z));probe.SetWidth(round(width*1e6))
        shape=probe.GetEffectiveShape(layer)
        for pad,required in guarded_pads:
            if required<=width+1e-6 or not pad.IsOnLayer(layer) or existing_entries.get(uid(pad)+':'+str(layer)):continue
            if not pad.GetEffectiveShape(layer).Collide(shape,1000):continue
            landing_ok=False
            for (la,endpoint),landing in root_landings.items():
                if la!=layer or min(math.dist(endpoint,a),math.dist(endpoint,z))>1e-6:continue
                full=k.PCB_TRACK(b);full.SetLayer(layer);full.SetStart(vec(landing[1]));full.SetEnd(vec(landing[2]));full.SetWidth(round(landing[3]*1e6))
                if witness(pad,full,required):landing_ok=True;break
            if not landing_ok:
                oracle.hits[uid(pad)]+=1
                return False
        return True
    @lru_cache(maxsize=None)
    def root_access(layer):
        w=trunk['outer_width_mm'] if layer in (k.F_Cu,k.B_Cu) else trunk['inner_width_mm']
        root_items=list(zz)
        if root_strategy=='all' and p.get('current_known') and trunk.get('current_known'):
            for fp in b.GetFootprints():
                for pad in fp.Pads():
                    if pad.GetNetname()!=net or uid(pad) in source_ids:continue
                    terminal=terminal_policy(fp.GetReference(),[pad.GetNumber()],net,rules)
                    if terminal and (terminal['rms_current_a']<p['rms_current_a'] or terminal['peak_current_a']<p['peak_current_a']):continue
                    root_items.append(pad)
        root_items=list({uid(item):item for item in root_items}.values())
        existing=power_access(trunk_anchors,layer,w,towards=[xy(source.GetPosition())]) if prefer_tree else []
        if root_strategy=='existing' and existing:
            qualified=qualified_tree_pads(b,net,layer,trunk,rules,trunk_anchors,source_ids)
            # These lands already lead to the full-current tree. Their explicit
            # terminal budget, not a generic class-width floor, sizes this access.
            for pad in qualified:
                terminal=terminal_policy(pad.GetParentFootprint().GetReference(),[pad.GetNumber()],net,rules)
                existing+=power_access([pad],layer,terminal['outer_width_mm'])
            return sorted(set(existing))
        # Grow a multi-terminal tree toward already full-current copper, rather
        # than forcing a low-current leaf through an unqualified isolated pad.
        branch_width=p['outer_width_mm'] if layer in (k.F_Cu,k.B_Cu) else p['inner_width_mm']
        if branch_width>=w-1e-9:return existing+power_access(root_items,layer,w,towards=[xy(source.GetPosition())])
        # A low-current branch may join a high-current terminal, but its landing
        # must still satisfy the terminal's full-width entry contract. Do not
        # route a narrow branch directly to a previously unqualified rail pad.
        points=existing+power_access([t for t in root_items if t.GetClass()!='PAD'],layer,w,towards=[xy(source.GetPosition())])
        from pnr.pad_entry import required_width
        for pad in (t for t in root_items if t.GetClass()=='PAD' and t.IsOnLayer(layer)):
            landing_width=max(branch_width,required_width(pad,rules))
            # A terminal attached by its qualified in-pad array (profile 5B) already
            # has its full-budget entry, so a declared branch joins its land at the
            # branch's own width (entry_clear likewise admits a qualified entry).
            if uid(pad) in array_attached():landing_width=branch_width
            for center in power_access([pad],layer,landing_width):
                if landing_width<=branch_width+1e-9:
                    points.append(center);continue
                for distance in (.25,.5,.75,1.):
                    for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                        end=(center[0]+dx*distance,center[1]+dy*distance)
                        if entry_clear(net,layer,center,end,landing_width):
                            points.append(end);root_landings[layer,end]=(layer,center,end,landing_width)
        return points
    layers=[k.F_Cu,k.B_Cu,k.In2_Cu]
    necks={};branch_counts={}
    # Terminal in-pad array attach (profile 5B only: legacy geometry has no in_pad
    # policy, so nothing below runs). Applies to a lone SMD terminal land whose
    # own layer has no budgeted surface access (full width or source-bounded
    # neck). Its current-sized row of filled 5B vias starts the trunk on another
    # layer; in_pad_ports maps (layer, start point) to that attach.
    g=oracle.geometry;in_pad_ports={};in_pad_state={}
    source_pads=[t for t in aa if t.GetClass()=='PAD']
    terminal_pad=(source_pads[0] if g.in_pad is not None and len(source_pads)==1 and p.get('terminal_sources')
                  and p.get('current_known') and source_pads[0].GetAttribute()==k.PAD_ATTRIB_SMD else None)
    terminal_layer=None
    if terminal_pad is not None:
        from pnr.via_in_pad import pad_layer
        terminal_layer=pad_layer(terminal_pad)
    @lru_cache(maxsize=None)
    def branch_access(layer):
        w=p['outer_width_mm'] if layer in (k.F_Cu,k.B_Cu) else p['inner_width_mm']
        points=power_access(aa,layer,w)
        if layer not in (k.F_Cu,k.B_Cu):return points+in_pad_access(layer)
        for pad in [t for t in aa if t.GetClass()=='PAD' and t.IsOnLayer(layer)]:
            center=xy(pad.GetPosition());minimum=max(min(xy(pad.GetSize())),rules.get('fab',{}).get('track_width_mm',.2))
            if minimum>=w:continue
            widths=sorted({minimum}|{i*.05 for i in range(math.ceil(minimum/.05),math.ceil(w/.05))})
            origins=[center]
            if pad.GetShape() in (k.PAD_SHAPE_RECT,k.PAD_SHAPE_ROUNDRECT,k.PAD_SHAPE_OVAL):
                sx,sy=xy(pad.GetSize());offset=max(sx,sy)/2-min(sx,sy)/2
                angle=math.radians(-pad.GetOrientation().AsDegrees())
                vx,vy=(offset,0) if sx>=sy else (0,offset)
                vx,vy=vx*math.cos(angle)-vy*math.sin(angle),vx*math.sin(angle)+vy*math.cos(angle)
                origins.extend([(center[0]+vx,center[1]+vy),(center[0]-vx,center[1]-vy)])
            for center in origins:
                for distance in (.15,.25,.35,.4,.5):
                    for nw in widths:
                        if nw>=w:continue
                        budget=neck_budget(p,nw,distance,rules['electrical_fab'])
                        if not budget:continue
                        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(.7071067812,.7071067812),(.7071067812,-.7071067812),(-.7071067812,.7071067812),(-.7071067812,-.7071067812)):
                            end=(center[0]+dx*distance,center[1]+dy*distance)
                            if not oracle.clear(net,layer,center,end,nw) or not entry_clear(net,layer,end,end,w):continue
                            neck_probe=k.PCB_TRACK(b);neck_probe.SetLayer(layer);neck_probe.SetStart(vec(center));neck_probe.SetEnd(vec(end));neck_probe.SetWidth(round(nw*1e6));shape=neck_probe.GetEffectiveShape(layer)
                            eligible=True
                            for touched,required in guarded_pads:
                                if required<=nw+1e-6 or not touched.IsOnLayer(layer) or existing_entries.get(uid(touched)+':'+str(layer)) or not touched.GetEffectiveShape(layer).Collide(shape,1000):continue
                                contract=terminal_policy(touched.GetParentFootprint().GetReference(),[touched.GetNumber()],net,rules)
                                if not contract or not neck_budget(contract,nw,distance,rules['electrical_fab']) or not witness(touched,neck_probe,nw):eligible=False;break
                            if eligible:
                                points.append(end);necks[layer,end]=(layer,center,end,nw,budget)
        points=sorted(set(points+in_pad_access(layer)));branch_counts[str(layer)]=len(points)
        return points
    def in_pad_access(layer):
        """Trunk start points on ``layer`` of the terminal's in-pad array attach."""
        if terminal_pad is None or layer==terminal_layer:return []
        if branch_access(terminal_layer):return []  # a budgeted surface attach exists
        plan_in_pad()
        return sorted(pt for la,pt in in_pad_ports if la==layer)
    def plan_in_pad():
        """Choose the array once: the engine's barrel-model count for the terminal
        budget (via_in_pad.array_requirement), 5B sites (attach_windows), every via
        through the native Oracle plus same-array hole spacing, and on each other
        power layer the full-width collector along the row whose copper covers every
        via disk (the trunk's first segment; via_in_pad.array_attach re-checks it)."""
        if in_pad_state:return
        from pnr.via_in_pad import array_requirement,attach_windows,layer_width,trunk_contact
        need=array_requirement(p,rules,g)
        in_pad_state.update(pad=terminal_pad.GetParentFootprint().GetReference()+'.'+terminal_pad.GetNumber(),
                            root_strategy=root_strategy,required=need['count'],windows=0,via_blocked=0,ports=0)
        # Diagnostic trail across the retries of this worker (result.json in_pad_attempts).
        oracle.__dict__.setdefault('in_pad_attempts',[]).append(in_pad_state)
        # Barrels this worker already validated and reserved (an earlier root strategy
        # of the same plan): re-checking them would collide with their own reservation.
        reserved=oracle.__dict__.setdefault('in_pad_reserved',set())
        for window in attach_windows(b,terminal_pad,need['count'],g):
            in_pad_state['windows']+=1;vias=window['vias']
            if any(math.dist(a[0],z[0])-(a[2]+z[2])/2<g.hole_gap-1e-9 for i,a in enumerate(vias) for z in vias[i+1:]):continue
            if not all((net,pt,d,h) in reserved or oracle.via(net,pt,d,h) for pt,d,h in vias):in_pad_state['via_blocked']+=1;continue
            diameter=max(d for _,d,_ in vias);first,last=vias[0][0],vias[-1][0];across=window['across']
            found=[]
            for la in layers:
                if la==terminal_layer:continue
                w=layer_width(p,la);reach=max(0.,(w-diameter)/2)
                offsets=sorted({0.}|{s*round(i*.01,6) for i in range(1,int(reach/.01+1e-9)+1) for s in (-1,1)}|({-reach,reach} if reach else set()),key=lambda s:(abs(s),s))
                chosen={}
                for s in offsets:
                    sides=[1,-1] if not s else [1 if s>0 else -1]
                    if all(side in chosen for side in sides):continue
                    a0=(round(first[0]+s*across[0],6),round(first[1]+s*across[1],6))
                    a1=(round(last[0]+s*across[0],6),round(last[1]+s*across[1],6))
                    if not all(trunk_contact(pt,d,a0,a1,w) for pt,d,_ in vias):continue
                    if not entry_clear(net,la,a0,a1,w):continue
                    for side in sides:chosen.setdefault(side,(s,a0,a1))
                for s,a0,a1 in dict.fromkeys(chosen.values()):
                    attach=dict(layer=la,vias=vias,collector=(la,a0,a1,w),
                                report=dict(pad=terminal_pad.GetParentFootprint().GetReference()+'.'+terminal_pad.GetNumber(),
                                            pad_uuid=uid(terminal_pad),layer=b.GetLayerName(la),variant=window['variant'],
                                            offsets_mm=window['offsets_mm'],neighbour_offsets_mm=window['neighbour_offsets_mm'],
                                            collector_offset_mm=s,trunk_width_mm=w,required=need['count'],
                                            vias=[dict(position=pt,diameter_mm=d,drill_mm=h) for pt,d,h in vias],
                                            rms_current_a=p['rms_current_a'],peak_current_a=p['peak_current_a']))
                    mid=(round((a0[0]+a1[0])/2,6),round((a0[1]+a1[1])/2,6))
                    for pt in dict.fromkeys((a0,a1,mid)):in_pad_ports.setdefault((la,pt),attach)
                    found.append(la)
            if found:
                # Later banks keep the drill spacing to these barrels (same-net holes).
                for pt,d,h in vias:
                    if (net,pt,d,h) not in reserved:reserved.add((net,pt,d,h));oracle.reserve_via(net,pt,d,h)
                in_pad_state.update(variant=window['variant'],layers=[b.GetLayerName(la) for la in dict.fromkeys(found)],
                                    ports=len(in_pad_ports))
                return
    def with_neck(plan):
        plan['root_strategy']=root_strategy
        # Add the selected terminal escape exactly once, never every candidate.
        endpoints={(la,tuple(pt)) for la,a,z,w in plan.get('tracks',[]) for pt in (a,z)}
        candidates=[v for key,v in necks.items() if key in endpoints]
        roots=[v for key,v in root_landings.items() if key in endpoints]
        if roots:
            landing=min(roots,key=lambda v:math.dist(v[1],v[2]));plan['tracks'].append(landing);plan['root_landing']=landing
        if candidates:
            chosen=min(candidates,key=lambda v:v[-1]['length_mm'])
            plan['tracks'].append(chosen[:4]);plan['neck']=chosen[-1]
        # The terminal's in-pad array attach, if the trunk starts at its collector.
        centres={tuple(center) for center,_,_ in plan.get('banks',[])}
        attaches=[v for key,v in in_pad_ports.items() if key in endpoints or key[1] in centres]
        if attaches:
            attach=attaches[0];la,a0,a1,w=attach['collector']
            if math.dist(a0,a1)>1e-8:plan['tracks'].append(attach['collector'])
            plan['in_pad_vias']=attach['vias'];plan['in_pad_array']=attach['report']
        return plan
    blocked=[]
    for la in layers:
        width=p['outer_width_mm'] if la in (k.F_Cu,k.B_Cu) else p['inner_width_mm']
        starts=branch_access(la);ends=root_access(la)
        if not starts or not ends:continue
        r=route(starts,ends,bounds,lambda a,z:entry_clear(net,la,a,z,width),pitch=pitch,max_expansions=12000)
        if r.status=='routed':return with_neck(dict(status='routed',tracks=[(la,a,z,width) for a,z in zip(r.path,r.path[1:])],banks=[],mode=p['mode'],policy=p))
        blocked.append(r.status)
    # Plane access first reuses the connected ground surface tree above. Only
    # source-budgeted transitions may create vias. Ground net without a net-wide
    # current contract can use an explicit terminal contract for this branch.
    if not p.get('via_array'):
        return dict(status='no_surface_channel' if blocked else 'no_qualified_power_access',mode=p['mode'],policy=p,detail='no source current/via budget for layer transition')
    if p['mode']=='plane':
        plane=b.GetLayerID(p['plane']);zones=[z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname()==net and z.IsOnLayer(plane)]
        for surface in (k.F_Cu,k.B_Cu):
            w=p['outer_width_mm'];starts=power_access(aa,surface,w);sites={}
            for a in starts:
                for radius in (.65,.85,1.1,1.5,2.,2.5,3.):
                    for angle in range(0,360,15):
                        center=(a[0]+radius*math.cos(math.radians(angle)),a[1]+radius*math.sin(math.radians(angle)))
                        if not(bounds[0]<=center[0]<=bounds[2] and bounds[1]<=center[1]<=bounds[3]):continue
                        if not any(z.GetFilledPolysList(plane).Contains(vec(center)) for z in zones):continue
                        ls=[(surface,w)];points=bank_clear(oracle,net,center,p,ls)
                        if not points:continue
                        sites[center]=points
                        for path in elbows(a,center):
                            if all(entry_clear(net,surface,x,y,w) for x,y in zip(path,path[1:])):
                                return dict(status='routed',tracks=[(surface,x,y,w) for x,y in zip(path,path[1:])],banks=[(center,points,ls)],mode=p['mode'],policy=p)
            if starts and sites:
                rr=route(starts,list(sites),bounds,lambda x,y:entry_clear(net,surface,x,y,w),pitch=pitch,max_expansions=12000)
                if rr.status=='routed':
                    center=tuple(rr.path[-1])
                    return dict(status='routed',tracks=[(surface,x,y,w) for x,y in zip(rr.path,rr.path[1:])],banks=[(center,sites[center],[(surface,w)])],mode=p['mode'],policy=p)
    # Use the layered search with width-specific clearance and aggregate via
    # bank footprints. Both ends may need layer transitions; never substitute a
    # 0.2-mm signal path or single via for a power path.
    from pnr.route.detail.layered import route_layers
    ls=[k.F_Cu,k.B_Cu,k.In2_Cu]
    widths=[p['outer_width_mm'],p['outer_width_mm'],p['inner_width_mm']]
    starts=set();ends=set();terminal_map=defaultdict(set)
    for index,la in enumerate(ls):
        for pt in branch_access(la):
            if bounds[0]<=pt[0]<=bounds[2] and bounds[1]<=pt[1]<=bounds[3]:starts.add(pt);terminal_map[pt].add(index)
        for pt in root_access(la):
            if bounds[0]<=pt[0]<=bounds[2] and bounds[1]<=pt[1]<=bounds[3]:ends.add(pt);terminal_map[pt].add(index)
    bank_cache={};port_cache={}
    def bank_site(pt):
        if pt not in bank_cache:
            # Barrel clearance still covers every crossed copper layer. Feed
            # tracks exist only on the two layers used by this transition.
            bank_cache[pt]=bank_clear(oracle,net,pt,p,[])
        return bool(bank_cache[pt])
    def bank_ports(pt,source_layer,target_layer):
        key=(pt,tuple(sorted((source_layer,target_layer))))
        if key not in port_cache:
            port_cache[key]=bank_clear(oracle,net,pt,p,
                [(ls[i],widths[i]) for i in key[1]]) if bank_site(pt) else None
        return bool(port_cache[key])
    for search_pitch in dict.fromkeys((max(.3,pitch),pitch)) if starts and ends else ():
        rr=route_layers(list(starts),list(ends),bounds,lambda index,a,z:entry_clear(net,ls[index],a,z,widths[index]),bank_site,pitch=search_pitch,layers=3,max_expansions=30000,max_vias=2,terminal_layers=lambda pt:tuple(terminal_map.get(tuple(pt),())),transition_clear=bank_ports,deadline=oracle.deadline)
        if rr.status=='routed':
            tracks=[];banks={}
            for a,z in zip(rr.path,rr.path[1:]):
                if a[2]==z[2]:tracks.append((ls[a[2]],a[:2],z[:2],widths[a[2]]))
                else:
                    center=tuple(a[:2]);ports=[(ls[i],widths[i]) for i in {a[2],z[2]}]
                    banks[center]=(center,port_cache[(center,tuple(sorted((a[2],z[2]))))],ports)
            return with_neck(dict(status='routed',tracks=tracks,banks=list(banks.values()),mode=p['mode'],policy=p))
    # Search compact banks near a qualified source and target. Short leads plus
    # a single bridge layer keep topology simple and auditable.
    outer=p['outer_width_mm'];inner=p['inner_width_mm']
    for source_layer in (k.F_Cu,k.B_Cu):
        starts=power_access(aa,source_layer,outer)
        if not starts:continue
        for bridge_layer in (k.B_Cu,k.In2_Cu,k.F_Cu):
            if bridge_layer==source_layer:continue
            width=inner if bridge_layer==k.In2_Cu else outer
            ends=root_access(bridge_layer)
            for a in sorted(starts,key=lambda a:math.dist(a,xy(target.GetPosition())))[:8]:
                for radius in (1.,1.5,2.,3.):
                    for dx,dy in ((1,0),(0,1),(-1,0),(0,-1)):
                        center=(a[0]+dx*radius,a[1]+dy*radius)
                        ls=[(source_layer,outer),(bridge_layer,width)]
                        points=bank_clear(oracle,net,center,p,ls)
                        if not points or not entry_clear(net,source_layer,a,center,outer):continue
                        if ends:
                            rr=route([center],ends,bounds,lambda x,y:entry_clear(net,bridge_layer,x,y,width),pitch=pitch,max_expansions=3000)
                            if rr.status=='routed':return with_neck(dict(status='routed',tracks=[(source_layer,a,center,outer)]+[(bridge_layer,x,y,width) for x,y in zip(rr.path,rr.path[1:])],banks=[(center,points,ls)],mode=p['mode'],policy=p))
    if prefer_tree and time.monotonic()<oracle.deadline:
        if root_strategy=='existing' and trunk.get('current_known'):
            return power_plan(b,net,source,target,rules,oracle,bounds,pitch,prefer_tree=True,root_strategy='all',_source_only=_source_only,_reverse_retry=_reverse_retry)
        if trunk_anchors:return power_plan(b,net,source,target,rules,oracle,bounds,pitch,prefer_tree=False,_source_only=_source_only,_reverse_retry=_reverse_retry)
    if _reverse_retry:
        result=power_plan(b,net,target,source,rules,oracle,bounds,pitch,prefer_tree=True,_source_only=True,_reverse_retry=False)
        result['reversed_branch_retry']=True
        return result
    return dict(status='no_current_sized_channel',mode=p['mode'],policy=p,branch_counts=branch_counts,neck_count=len(necks))


def bridge_half_plane(a,z,hand):
    if hand not in (-1,0,1):raise ValueError('bridge hand must be -1,0,+1')
    dx,dy=z[0]-a[0],z[1]-a[1]
    return lambda q:not hand or hand*(dx*(q[1]-a[1])-dy*(q[0]-a[0]))>=-1e-9


def pair_topologies(pair):
    auxiliary=pair.get('auxiliary_pairs',[])
    candidates=[]
    # Only the first declared connector group has interchangeable duplicate
    # contacts. Intermediate protection/load terminals remain in source order.
    if auxiliary and all('source' in g and 'target' in g for g in auxiliary):
        for takeoff in ('declared','auxiliary_source','declared_p_source_n','source_p_declared_n'):
            for hand in (-1,1):
                candidates.append(dict(bridge_hand=hand,takeoff=takeoff,auxiliary_order=('p','n')))
    for order in ([('p','n'),('n','p')] if auxiliary else [('p','n')]):
        candidates.append(dict(bridge_hand=0,takeoff='auxiliary_source',auxiliary_order=order))
    return candidates


def pair_plan(b,pair,rules,oracle,bounds,pitch):
    """Compare bounded connector embeddings and takeoffs, without net swapping."""
    deadline=oracle.deadline;configs=pair_topologies(pair);attempts=[];hits=Counter();via_hits=Counter();best=None;retained=[];last={}
    joint_count=0;joint_deadline=time.monotonic();joint_trial_seconds=90.
    if os.environ.get('PNR_PAIR_JOINT_TOPOLOGIES','0')=='1':
        from pnr.pair_joint import joint_topologies
        joint_trial_seconds=float(os.environ.get('PNR_PAIR_JOINT_TRIAL_SECONDS','90'))
        if not math.isfinite(joint_trial_seconds) or joint_trial_seconds<=0:raise ValueError('invalid joint topology trial seconds')
        positions={f.GetReference()+'.'+pad.GetNumber():xy(pad.GetPosition()) for f in b.GetFootprints() for pad in f.Pads()}
        joint=joint_topologies(pair,positions,max_trials=int(os.environ.get('PNR_PAIR_JOINT_MAX_TRIALS','6')),budget_scope=os.environ.get('PNR_PAIR_AUXILIARY_SCOPE','separate'))
        joint_count=len(joint);configs=joint+configs
        now=time.monotonic();remaining=max(0.,deadline-now)
        # Keep a bounded fallback reserve. Do not divide one viable 60-second
        # joint search into eighteen subsecond trials merely to exhaust seeds.
        joint_deadline=now+(remaining-min(30.,remaining/3) if math.isfinite(remaining) else joint_count*joint_trial_seconds)
    for index,config in enumerate(configs):
        now=time.monotonic()
        if now>=deadline:break
        if index<joint_count:
            if now>=joint_deadline:
                attempts.append(dict(config,status='joint_phase_budget',elapsed_seconds=0.));continue
            trial_deadline=min(deadline,joint_deadline,now+joint_trial_seconds)
        else:trial_deadline=now+(deadline-now)/(len(configs)-index)
        trial=oracle.fork(trial_deadline);retained.append(trial)
        order=config['auxiliary_order']
        try:
            if config['bridge_hand']:
                result=_pair_plan_order(b,pair,rules,trial,bounds,pitch,order,config)
            else:result=_pair_plan_order(b,pair,rules,trial,bounds,pitch,order)
        except TimeoutError:result=dict(status='time_budget',mode='pair')
        last=result;hits.update(trial.hits);via_hits.update(trial.via_hits)
        attempt=dict(config,status=result['status'],elapsed_seconds=time.monotonic()-now);attempts.append(attempt)
        for key in ('failed_stage','endpoint_metrics','connector_endpoint_metrics'):
            if key in result:attempt[key]=result[key]
        if result['status']=='routed':
            score=(len(result.get('pair_vias',[])),sum(math.dist(a,z) for _,_,a,z,_ in result.get('pair_tracks',[])))
            attempt['route_score']=score
            if best is None or score<best[0]:best=(score,result,trial)
    if best:
        _,result,trial=best;oracle.__dict__.update(trial.__dict__);oracle.deadline=deadline;oracle.hits=hits;oracle.via_hits=via_hits
        return dict(result,order_attempts=attempts)
    oracle.hits.update(hits);oracle.via_hits.update(via_hits)
    return dict(last or dict(status='time_budget',mode='pair'),order_attempts=attempts)




def move_pair_support(b,pair,spec):
    """Translate an intermediate pair device and its isolated plane-return fanout.

    Connector/receiver positions stay fixed. Existing pair copper is replaced
    atomically by the caller; other-net layer ports or shared returns forbid a
    translation. Constraint legality is checked by the controller before this
    worker, and full native connectivity/DRC after the pair is rebuilt.
    """
    import pcbnew as k
    from pnr.plane_access import surface_group
    intermediate={v.rsplit('.',1)[0] for t in pair.get('terminal_chain',[])[1:-1] for v in t.values()}
    ref=spec['ref']
    if ref not in intermediate:raise ValueError('only source-declared intermediate pair devices may move')
    f=next(f for f in b.GetFootprints() if f.GetReference()==ref)
    if f.IsLocked() or math.dist(xy(f.GetPosition()),spec['original'])>1e-6:raise ValueError('stale/locked pair placement')
    old_rotation=f.GetOrientationDegrees()
    if abs(old_rotation-spec.get('original_rotation',old_rotation))>1e-6:raise ValueError('stale pair orientation')
    other=[p for p in f.Pads() if p.GetNetname() not in (pair['p'],pair['n']) and p.GetNetCode()]
    moved={}
    for pad in other:
        if pad.GetAttribute()!=k.PAD_ATTRIB_SMD:raise ValueError('pair support has non-SMD port')
        group=surface_group(b,[pad],pad.GetLayer());ids={uid(t) for t in group}
        if any(t.GetClass()=='PAD' and t.GetParentFootprint().GetReference()!=ref for t in group):raise ValueError('shared surface return')
        for t in group:
            if t.GetClass()=='PAD':continue
            if t.IsLocked():raise ValueError('locked support copper')
            if t.GetClass()=='PCB_VIA':
                for external in b.GetTracks():
                    if uid(external) in ids or external.GetClass()=='PCB_VIA' or external.GetNetCode()!=t.GetNetCode():continue
                    if t.GetEffectiveShape(external.GetLayer()).Collide(external.GetEffectiveShape(external.GetLayer()),0):raise ValueError('preserve support via external port')
            moved[uid(t)]=t
    delta=vec((spec['position'][0]-spec['original'][0],spec['position'][1]-spec['original'][1]))
    f.SetPosition(vec(spec['position']))
    rotation=spec.get('rotation',old_rotation)
    f.SetOrientationDegrees(rotation)
    for t in moved.values():
        t.Move(delta)
        t.Rotate(vec(spec['position']),k.EDA_ANGLE(rotation-old_rotation,k.DEGREES_T))
    b.BuildConnectivity()
    return dict(ref=ref,original=spec['original'],position=spec['position'],original_rotation=old_rotation,rotation=rotation,translated_return_items=sorted(moved))

def main():
    import pcbnew as k
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('board',type=Path);ap.add_argument('--rules',type=Path,required=True);ap.add_argument('--out-dir',type=Path,required=True);ap.add_argument('--source-pad',required=True);ap.add_argument('--target-pad',required=True);ap.add_argument('--net',required=True);ap.add_argument('--bounds',nargs=4,type=float,required=True);ap.add_argument('--seconds',type=float,default=20);ap.add_argument('--pitch',type=float,default=.15);ap.add_argument('--kicad-cli',required=True);ap.add_argument('--placement-spec',type=Path);ap.add_argument('--placement-candidates',type=Path)
    ap.add_argument('--source-pad-uuid');ap.add_argument('--target-pad-uuid')
    a=ap.parse_args();a.out_dir.mkdir(parents=True,exist_ok=False)
    from pnr.fab_profile import load_board  # custom rules in force for the refills below
    rules=json.loads(a.rules.read_text());b=load_board(a.board);b.BuildConnectivity();groups=partition(b);entries=snapshot(b,rules)
    policy=net_policy(a.net,rules)
    if a.placement_candidates:
        proposals=screen_pair_placements(a.board,rules,policy['pair'],json.loads(a.placement_candidates.read_text()),a.bounds)
        (a.out_dir/'result.json').write_text(json.dumps(dict(status='placement_screen',proposals=proposals),indent=2))
        return
    from pnr.pad_identity import resolve_pad
    source=resolve_pad(b,a.source_pad,a.source_pad_uuid)
    target=resolve_pad(b,a.target_pad,a.target_pad_uuid)
    if any(uid(source) in g and uid(target) in g for g in groups):
        (a.out_dir/'result.json').write_text(json.dumps(dict(status='already_connected',accepted=False))+'\n')
        return
    if source.GetNetname()!=a.net or target.GetNetname()!=a.net:raise ValueError('net/terminal mismatch')
    removed=[];placement=None
    if policy['mode']=='pair':
        pair=policy['pair'];removed=[t for t in b.GetTracks() if t.GetNetname() in (pair['p'],pair['n'])]
        if any(t.IsLocked() for t in removed):raise ValueError('pair copper locked')
        for t in removed:b.Remove(t)
        b.BuildConnectivity()
        if a.placement_spec:
            try:placement=move_pair_support(b,pair,json.loads(a.placement_spec.read_text()))
            except ValueError as exc:
                (a.out_dir/'result.json').write_text(json.dumps(dict(status='pair_placement_guard',accepted=False,reason=str(exc)))+'\n');return
    if policy['mode']=='pair':
        k.ZONE_FILLER(b).Fill(b.Zones());b.BuildConnectivity()
    oracle=Oracle(b,rules,deadline=time.monotonic()+a.seconds)
    # Profile only (5A/5B via-to-SMD-pad rule; legacy has none and KiCad DRC is its
    # whole rule): vias breaking the rule before this transaction.
    from pnr.via_in_pad import forbidden_vias
    forbidden_before=forbidden_vias(b,oracle.geometry) if oracle.geometry.via_to_smd_pad is not None else None
    try:
        plan=pair_plan(b,policy['pair'],rules,oracle,a.bounds,a.pitch) if policy['mode']=='pair' else power_plan(b,a.net,source,target,rules,oracle,a.bounds,a.pitch)
    except TimeoutError:plan=dict(status='time_budget',mode=policy['mode'])
    plan.update(accepted=False,placement=placement,static_blockers=dict(oracle.hits.most_common(30)),via_blockers=dict(oracle.via_hits.most_common(30)))
    if getattr(oracle,'in_pad_attempts',None):plan['in_pad_attempts']=oracle.in_pad_attempts
    keep=[]
    if plan['status']=='routed':
        for la,x,y,w in plan.get('tracks',[]):keep.append(add_track(b,a.net,la,x,y,w))
        for center,points,layers in plan.get('banks',[]):keep.extend(add_bank(b,a.net,center,points,plan['policy'],layers))
        if plan.get('in_pad_vias'):keep.extend(add_in_pad_vias(b,a.net,plan['in_pad_vias'],oracle.geometry))
        for net,la,x,y,w in plan.get('pair_tracks',[]):keep.append(add_track(b,net,la,x,y,w))
        for net,pt in plan.get('pair_vias',[]):
            v=k.PCB_VIA(b);v.SetNetCode(b.FindNet(net).GetNetCode());v.SetPosition(vec(pt));v.SetFrontWidth(round(plan['via_diameter_mm']*1e6));v.SetDrill(round(plan['via_drill_mm']*1e6));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu);b.Add(v);keep.append(v)
        b.BuildConnectivity()
        from pnr.pad_entry import repair_changed_entries
        plan.update(repair_changed_entries(b,rules,entries))
        b.BuildConnectivity();k.ZONE_FILLER(b).Fill(b.Zones());b.BuildConnectivity()
        if plan.get('mode')=='pair':
            pair=policy['pair'];reference=pair_reference_validator(b,pair,rules)
            reference_checks=[reference(segment['reference_paths'],0) for segment in plan['segments']]
            plan['postfill_reference_checks']=reference_checks
            if not all(reference_checks):
                plan['status']='pair_postfill_reference_discontinuity'
        current=snapshot(b,rules)
        checks=dict(preserved=preserved(groups,partition(b)),lost_pad_entries=[i for i,v in entries.items() if v and not current.get(i,False)],new_bad_entries=[i for i,v in current.items() if not v and i not in entries])
        checks['reference_failures']=reference_failures(b,rules)
        if forbidden_before is not None:
            # 5B geometry KiCad cannot see (the DRU exempts the 0.20-drill class).
            checks['new_forbidden_smd_vias']=sorted(forbidden_vias(b,oracle.geometry)-forbidden_before)
        if plan.get('in_pad_vias'):
            # The array is the terminal attach only if it qualifies as built: 5B
            # vias in the pad, one full-width trunk on the other layer, capacity.
            from pnr.via_in_pad import array_attach
            terminal=next(p for f in b.GetFootprints() for p in f.Pads() if uid(p)==plan['in_pad_array']['pad_uuid'])
            checks['in_pad_attach']=array_attach(b,terminal,rules,oracle.geometry)
        output=a.out_dir/'candidate.kicad_pcb';k.SaveBoard(str(output),b);shutil.copyfile(a.board.with_suffix('.kicad_pro'),output.with_suffix('.kicad_pro'))
        table=a.board.parent/'fp-lib-table'
        if table.exists():(a.out_dir/'fp-lib-table').write_text(table.read_text().replace('${KIPRJMOD}',str(a.board.parent.resolve())))
        def drc(board,name):
            from pnr.native_drc import run_drc
            return run_drc(a.kicad_cli,board,a.out_dir/name)
        before=drc(a.board,'baseline.drc.json');after=drc(output,'candidate.drc.json')
        plan.update(checks=checks,before_opens=len(before['unconnected_items']),after_opens=len(after['unconnected_items']),accepted=plan['status']=='routed' and not checks['reference_failures'] and acceptable(before,after,checks) and not checks['new_bad_entries'] and len(after['unconnected_items'])<len(before['unconnected_items'])
                    and not checks.get('new_forbidden_smd_vias') and (not plan.get('in_pad_vias') or bool((checks.get('in_pad_attach') or {}).get('qualified'))))
    if not plan['accepted']:
        diagnostic=a.out_dir/'diagnostic-NOT-ACCEPTED.kicad_pcb'
        k.SaveBoard(str(diagnostic),b)
        shutil.copyfile(a.board.with_suffix('.kicad_pro'),diagnostic.with_suffix('.kicad_pro'))
    (a.out_dir/'result.json').write_text(json.dumps(plan,indent=2)+'\n')
    print(json.dumps({k:v for k,v in plan.items() if k not in ('static_blockers','tracks','banks','pair_tracks')}))



def pair_via_geometry(rules):
    fab=rules.get('fab',{});electrical=rules.get('electrical_fab',{})
    diameter=float(fab.get('via_diameter_mm',electrical.get('via_diameter_mm',.6)))
    drill=float(fab.get('via_drill_mm',electrical.get('via_drill_mm',.3)))
    if not 0<drill<diameter:raise ValueError('invalid pair via dimensions')
    return diameter,drill

def pair_bridge_ports(pair,terminals,rules,oracle,bounds,index):
    """Exact paired surface fanouts and all-layer legal via sites."""
    import pcbnew as k
    from pnr.route.detail.coupled import geometry_ok
    from pnr.route.detail.regional import segment_distance
    p,n=pair['p'],pair['n'];width,gap=pair['width_mm'],pair['gap_mm']
    diameter,drill=pair_via_geometry(rules)
    clearance=max(net_policy(net,rules)['clearance_mm'] for net in (p,n))
    hole_gap=rules.get('fab',{}).get('hole_to_hole_mm',rules.get('fab',{}).get('hole_clearance_mm',.2))
    via_spacing=max(diameter+clearance+.002,drill+hole_gap+.002)
    cap=pair.get('max_uncoupled_mm',2)
    a,z=terminals[p][index],terminals[n][index];distance=math.dist(a,z)
    if distance<1e-8:return []
    axis=((a[0]-z[0])/distance,(a[1]-z[1])/distance)
    tangent=(-axis[1],axis[0]);mid=tuple((a[i]+z[i])/2 for i in (0,1));found=[]
    for run in sorted(set((.8,1.,1.2,1.5,1.75)) | {round(i*.1,10) for i in range(4,18)}):
        for sign in (-1,1):
            for spacing in sorted({via_spacing,max(via_spacing,distance)}):
                for shift in (0,-.25,.25,-.5,.5):
                    center=tuple(mid[i]+sign*tangent[i]*run+shift*axis[i] for i in (0,1))
                    sites={net:tuple(center[i]+side*axis[i]*spacing/2 for i in (0,1)) for net,side in ((p,1),(n,-1))}
                    if any(not(bounds[0]<=v[0]<=bounds[2] and bounds[1]<=v[1]<=bounds[3]) for v in sites.values()):continue
                    if not all(oracle.via(net,point,diameter,drill) for net,point in sites.items()):continue
                    choices={net:[path for path in elbows(terminals[net][index],sites[net]) if length(path)<cap-.15 and all(oracle.clear(net,k.F_Cu,x,y,width,pair=pair) for x,y in zip(path,path[1:]))] for net in (p,n)}
                    for pp in choices[p]:
                        for nn in choices[n]:
                            paths={p:pp,n:nn}
                            if not geometry_ok(paths,width,gap,lambda net,x,y,w:oracle.clear(net,k.F_Cu,x,y,w,pair=pair)):continue
                            if any(segment_distance(sites[other],sites[other],x,y)<(diameter+width)/2+clearance+.001 for net,other in ((p,n),(n,p)) for x,y in zip(paths[net],paths[net][1:])):continue
                            found.append(dict(shift_mm=shift,sites=sites,paths=paths,lengths={net:length(path) for net,path in paths.items()}));break
                        else:continue
                        break
    return found

def pair_reference_validator(board,pair,rules,prospective_vias=(),base_center=None):
    """Validate actual tuned trunks against the saved filled reference copper."""
    import pcbnew as k
    from pnr.route.detail.coupled import trim_path
    reference=board.GetLayerID(pair.get('reference_layer','In1.Cu'))
    nets={n for c in rules.get('net_classes',[]) if c.get('plane_layer')==pair.get('reference_layer','In1.Cu') for n in c['nets']}
    fill=k.SHAPE_POLY_SET()
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.IsOnLayer(reference) and z.GetNetname() in nets:fill.BooleanAdd(z.GetFilledPolysList(reference))
    # A newly drilled signal via removes reference copper after the final fill.
    # Include its clearance aperture during route search, not only afterwards.
    zones=[z for z in board.Zones() if not z.GetIsRuleArea() and z.IsOnLayer(reference) and z.GetNetname() in nets]
    diameter,_=pair_via_geometry(rules)
    clearance=max([float(z.GetLocalClearance())/1e6 for z in zones]+[rules.get('fab',{}).get('clearance_mm',.2)])
    aperture_boxes=[]
    for net,point in prospective_vias:
        if net in nets:continue
        probe=k.PCB_TRACK(board);probe.SetLayer(k.F_Cu);probe.SetStart(vec(point));probe.SetEnd(vec(point));probe.SetWidth(round((diameter+2*clearance+.004)*1e6))
        aperture=k.SHAPE_POLY_SET();probe.TransformShapeToPolygon(aperture,k.F_Cu,0,1000,k.ERROR_OUTSIDE);fill.BooleanSubtract(aperture)
        box=aperture.BBox();aperture_boxes.append((box.GetLeft()/1e6,box.GetTop()/1e6,box.GetRight()/1e6,box.GetBottom()/1e6))
    from functools import lru_cache
    @lru_cache(maxsize=100000)
    def center(a,z,width):
        if fill.IsEmpty():return False
        # Far from all new apertures, containment equals the unchanged base
        # reference. Use actual native aperture bounds plus outward error guard.
        # Near apertures, retain the exact native subtraction path below.
        margin=width/2+.002
        if base_center is not None and all(max(a[0],z[0])+margin<x0 or min(a[0],z[0])-margin>x1 or max(a[1],z[1])+margin<y0 or min(a[1],z[1])-margin>y1 for x0,y0,x1,y1 in aperture_boxes):
            return base_center(a,z,width)
        probe=k.PCB_TRACK(board);probe.SetLayer(k.F_Cu);probe.SetStart(vec(a));probe.SetEnd(vec(z));probe.SetWidth(round(width*1e6))
        poly=k.SHAPE_POLY_SET();probe.TransformShapeToPolygon(poly,k.F_Cu,0,1000,k.ERROR_OUTSIDE);poly.BooleanSubtract(fill)
        return poly.IsEmpty()
    def valid(paths,cap):
        if fill.IsEmpty():return False
        for path in paths.values():
            trimmed=trim_path(path,cap,cap)
            if any(not center(tuple(a),tuple(z),pair['width_mm']+pair['gap_mm']) for a,z in zip(trimmed,trimmed[1:])):return False
        return True
    valid.available=not fill.IsEmpty()
    valid.center=center
    return valid

def reference_failures(board,rules):
    """Recheck accepted pair trunk witnesses after any later copper/refill edit."""
    pairs={p['name']:p for p in rules.get('diff_pairs',[])};failures=[]
    for ref in rules.get('routed_pair_references',[]):
        pair=pairs[ref['pair']];valid=pair_reference_validator(board,pair,rules)
        for index,segment in enumerate(ref['segments']):
            if not valid(segment['reference_paths'],0):failures.append(dict(pair=pair['name'],segment=index))
    return failures


def pair_layer_bridge(b,pair,terminals,rules,oracle,bounds,pitch,offsets,reuse_source=None,reference_validator=None,prior_reference=(),prior_vias=(),solution_index=0,max_expansions=15000,timing_target_mm=0):
    """Matched through-via pairs and a checked B.Cu trunk, no single-leg jump.

    The combined surface and bridge-plane fanout obeys the original planar
    uncoupled budget. Equal barrel lengths enter endpoint timing. Impedance and
    transition qualification remain explicitly pending the actual stackup.
    """
    import pcbnew as k
    from pnr.route.detail.coupled import geometry_ok
    from pnr.route.detail.regional import segment_distance
    p,n=pair['p'],pair['n'];width,gap=pair['width_mm'],pair['gap_mm']
    diameter,drill=pair_via_geometry(rules)
    clearance=max(net_policy(net,rules)['clearance_mm'] for net in (p,n))
    hole_gap=rules.get('fab',{}).get('hole_to_hole_mm',rules.get('fab',{}).get('hole_clearance_mm',.2))
    via_spacing=max(diameter+clearance+.002,drill+hole_gap+.002)
    cap=pair.get('max_uncoupled_mm',2)
    ports=lambda index:pair_bridge_ports(pair,terminals,rules,oracle,bounds,index)
    starts,ends=([reuse_source] if reuse_source else ports(0)),ports(1)
    candidates=sorted(((a,z) for a in starts for z in ends),key=lambda az:(abs(az[0].get('shift_mm',0))+abs(az[1].get('shift_mm',0)),sum(az[0]['lengths'].values())+sum(az[1]['lengths'].values())+sum(math.dist(az[0]['sites'][net],az[1]['sites'][net]) for net in (p,n))))
    bridge_failures=Counter();examples=[];successful=0
    for a,z in candidates[:64]:
        vias=[(net,group['sites'][net]) for group in (a,z) for net in (p,n)]
        if any(math.dist(v,q)+1e-6<(via_spacing if net!=other else drill+hole_gap+.002) for i,(net,v) in enumerate(vias) for other,q in vias[i+1:]):continue
        def clear(net,x,y,w):
            return oracle.clear(net,k.B_Cu,x,y,w,pair=pair) and all(other==net or segment_distance(x,y,pt,pt)>=(diameter+w)/2+clearance+.001 for other,pt in vias)
        candidate_reference=pair_reference_validator(b,pair,rules,list(prior_vias)+vias,base_center=reference_validator.center) if reference_validator else None
        if candidate_reference and any(not candidate_reference(paths,0) for paths in prior_reference):continue
        remaining=cap-max(max(a['lengths'].values())+a.get('entry_budget_mm',0),max(z['lengths'].values()))
        if remaining<=0:continue
        totals={net:offsets.get(net,0)+a['lengths'][net]+z['lengths'][net]+(1 if a.get('reuse') else 2)*rules['electrical_fab']['board_thickness_mm'] for net in (p,n)}
        tuning_offsets=dict(totals);tuning_offsets[n]+=timing_target_mm
        rr=solve_pair(p,n,{net:(a['sites'][net],z['sites'][net]) for net in (p,n)},bounds,clear,lambda x,y,w:oracle.clear(p,k.B_Cu,x,y,w,ignore_nets=(p,n)) and (candidate_reference is None or candidate_reference.center(tuple(x),tuple(y),w+gap)),width,gap,pair['skew_mm'],pitch=pitch,max_expansions=max_expansions,max_uncoupled=remaining,max_tuning_length=cap,offsets=tuning_offsets,accept_paths=(lambda paths:candidate_reference(paths,remaining)) if candidate_reference else None)
        if rr['status']!='routed':
            bridge_failures.update(rr.get('failures',{}))
            if len(examples)<8:examples.append(dict(source=a,target=z,remaining=remaining,result=rr))
            continue
        if successful<solution_index:
            successful+=1
            continue
        rr['lengths'][n]-=timing_target_mm
        rr['intermediate_timing_target_mm']=timing_target_mm
        tracks=[]
        for net in (p,n):
            for group in (a,z):tracks.extend((net,k.F_Cu,x,y,width) for x,y in zip(group['paths'][net],group['paths'][net][1:]))
            tracks.extend((net,k.B_Cu,x,y,width) for x,y in zip(rr['paths'][net],rr['paths'][net][1:]))
        from pnr.route.detail.coupled import trim_path
        rr['reference_paths']={net:trim_path(path,remaining,remaining) for net,path in rr['paths'].items()}
        rr.update(pair_tracks=tracks,pair_vias=[(net,group['sites'][net]) for group in (a,z) if not group.get('reuse') for net in (p,n)],bridge_target=z,selected_port_rank=successful,layer='B.Cu',via_diameter_mm=diameter,via_drill_mm=drill,fanout_lengths=[a['lengths'],z['lengths']])
        return rr
    return dict(status='pair_no_matched_layer_bridge',source_ports=len(starts),target_ports=len(ends),attempts=min(len(candidates),64),failures=dict(bridge_failures),examples=examples)

def duplicate_endpoint_metrics(pair,bylabel,tracks,vias,thickness):
    """Measure every declared connector contact, including actual branch joins."""
    import pcbnew as k
    origins=[pair['terminal_chain'][0]]
    origins.extend(group['source'] for group in pair.get('auxiliary_pairs',[]))
    origins.extend(group['target'] for group in pair.get('auxiliary_pairs',[]))
    result={}
    for origin in origins:
        name=origin['p']+'/'+origin['n']
        if name in result:continue
        values={}
        for net,key in ((pair['p'],'p'),(pair['n'],'n')):
            start=bylabel[origin[key]];end=bylabel[pair['terminal_chain'][-1][key]]
            values[net]=path_metrics([(la,a,z) for nn,la,a,z,w in tracks if nn==net],[(pt,[k.F_Cu,k.B_Cu]) for nn,pt in vias if nn==net],(xy(start.GetPosition()),k.F_Cu),(xy(end.GetPosition()),k.F_Cu),layer_heights={k.F_Cu:0,k.B_Cu:thickness})
        result[name]=values
    return result


def connector_origins_qualified(pair,metrics):
    return bool(metrics) and all(all(m.get('valid',False) for m in values.values()) and abs(values[pair['p']]['length_mm']-values[pair['n']]['length_mm'])<=pair['skew_mm']+1e-6 for values in metrics.values())


def _pair_plan_order(b,pair,rules,oracle,bounds,pitch,auxiliary_order,topology=None):
    import pcbnew as k
    topology=topology or {};hand=topology.get('bridge_hand',0);takeoff=topology.get('takeoff','auxiliary_source')
    p,n=pair['p'],pair['n'];pads=[pad for f in b.GetFootprints() for pad in f.Pads() if pad.GetNetname() in (p,n)]
    groups=defaultdict(lambda:defaultdict(list))
    for pad in pads:groups[pad.GetParentFootprint().GetReference()][pad.GetNetname()].append(pad)
    # Native topology is explicit: each package must expose both polarities.
    if any(set(g)!={p,n} for g in groups.values()):return dict(status='pair_terminal_topology_unsupported')
    # Source annotations supply an ordered pair path through connector/ESD/load.
    endpoints=pair.get('terminal_chain',[])
    if len(endpoints)<2:return dict(status='missing_pair_terminal_chain')
    paths={p:[],n:[]};tracks=[];metrics=[];vias=[]
    la=k.F_Cu
    reference_validator=pair_reference_validator(b,pair,rules)
    if not reference_validator.available:return dict(status="pair_reference_plane_discontinuity")
    accumulated={p:0,n:0}
    # Duplicate USB-C contacts are explicitly bounded local branches, not a
    # second independently routed long pair. Each branch still needs a native
    # path and an endpoint timing check; crossovers may use checked vias.
    from pnr.route.detail.layered import route_layers
    auxiliary=[];branch_paths=[];joint_source_port=None
    diameter,drill=pair_via_geometry(rules)
    bylabel={pad.GetParentFootprint().GetReference()+'.'+pad.GetNumber():pad for pad in pads}
    for group in pair.get('auxiliary_pairs',[]):
        lengths={};group_paths={}
        for key in auxiliary_order:
            net=pair[key]
            first,last=bylabel[group['source'][key]],bylabel[group['target'][key]]
            a,z=xy(first.GetPosition()),xy(last.GetPosition())
            region=[min(a[0],z[0])-2,min(a[1],z[1])-2,max(a[0],z[0])+2,max(a[1],z[1])+2]
            # Mirrored embeddings constrain the short duplicate-contact bridge
            # to either side of its own chord. This is rigid-transform invariant
            # and keeps literal net/pad assignments; it never swaps polarity.
            side=bridge_half_plane(a,z,hand)
            clear_branch=lambda i,x,y:side(x) and side(y) and oracle.clear(net,(k.F_Cu,k.B_Cu)[i],x,y,pair['width_mm'],pair=pair)
            if topology.get('bridge_depth_mm'):
                from types import SimpleNamespace
                dx,dy=z[0]-a[0],z[1]-a[1];span=math.hypot(dx,dy);depth=topology['bridge_depth_mm']
                mid=((a[0]+z[0])/2-hand*dy/span*depth,(a[1]+z[1])/2+hand*dx/span*depth)
                left=route_layers([a],[mid],region,clear_branch,lambda pt:False,pitch=.05,layers=2,max_expansions=5000,max_vias=0,deadline=oracle.deadline)
                right=route_layers([mid],[z],region,clear_branch,lambda pt:False,pitch=.05,layers=2,max_expansions=5000,max_vias=0,deadline=oracle.deadline)
                rr=SimpleNamespace(status='routed' if left.status==right.status=='routed' else 'no_channel',path=left.path[:-1]+right.path)
            else:
                rr=route_layers([a],[z],region,clear_branch,lambda pt:side(pt) and oracle.via(net,pt,diameter,drill),pitch=.1,layers=2,max_expansions=5000,max_vias=2,deadline=oracle.deadline)

            if rr.status!='routed':return dict(status='pair_auxiliary_'+rr.status)
            total=sum(math.dist(a[:2],z[:2]) if a[2]==z[2] else rules['electrical_fab']['board_thickness_mm'] for a,z in zip(rr.path,rr.path[1:]))
            if total>group['max_length_mm']:return dict(status='pair_auxiliary_length_limit',length_mm=total)
            lengths[net]=total;group_paths[net]=rr.path
            for a,z in zip(rr.path,rr.path[1:]):
                if a[2]==z[2]:
                    layer=(k.F_Cu,k.B_Cu)[a[2]]
                    tracks.append((net,layer,a[:2],z[:2],pair['width_mm']))
                    oracle.reserve_track(net,layer,a[:2],z[:2],pair['width_mm'])
                else:
                    vias.append((net,a[:2]));oracle.reserve_via(net,a[:2],diameter,drill)
        if abs(lengths[p]-lengths[n])>pair['skew_mm']:return dict(status='pair_auxiliary_skew',lengths=lengths)
        auxiliary.append(lengths);branch_paths.append(group_paths)
    routing_endpoints=[dict(t) for t in endpoints]
    for group,branch_lengths in zip(pair.get('auxiliary_pairs',[]),auxiliary):
        if group['target']==routing_endpoints[0]:
            keys={'auxiliary_source':('p','n'),'declared_p_source_n':('n',),'source_p_declared_n':('p',)}.get(takeoff,())
            for key in keys:
                routing_endpoints[0][key]=group['source'][key];accumulated[pair[key]]+=branch_lengths[pair[key]]
    if takeoff=='bridge_join_via':
        from pnr.pair_joint import branch_join_port
        groups=list(zip(pair.get('auxiliary_pairs',[]),branch_paths))
        found=next((paths for group,paths in groups if group['target']==endpoints[0]),None)
        if found is None:return dict(status='pair_joint_missing_connector_group')
        joint=branch_join_port(pair,found,oracle,rules,topology.get('join_fraction',.5),budget_scope=topology.get('auxiliary_budget_scope','combined'))
        if joint['status']!='routed':return joint
        joint_source_port=joint['port'];accumulated=joint['declared_offsets']
    for stage,(first,last) in enumerate(zip(routing_endpoints,routing_endpoints[1:])):
        if stage:
            actual={} 
            for net,key in ((p,'p'),(n,'n')):
                origin,finish=bylabel[endpoints[0][key]],bylabel[first[key]]
                metric=path_metrics([(la,a,z) for nn,la,a,z,w in tracks if nn==net],[(pt,[k.F_Cu,k.B_Cu]) for nn,pt in vias if nn==net],(xy(origin.GetPosition()),k.F_Cu),(xy(finish.GetPosition()),k.F_Cu),layer_heights={k.F_Cu:0,k.B_Cu:rules['electrical_fab']['board_thickness_mm']})
                if not metric.get('valid'):return dict(status='pair_partial_endpoint_graph_invalid',partial_metric=metric)
                actual[net]=metric['length_mm']
            accumulated=actual
        terminals={}
        for net,key in ((p,'p'),(n,'n')):
            def find(label):
                ref,num=label.rsplit('.',1)
                found=[pad for pad in pads if pad.GetParentFootprint().GetReference()==ref and pad.GetNumber()==num]
                if len(found)!=1:raise ValueError('ambiguous pair endpoint '+label)
                return found[0]
            a,z=find(first[key]),find(last[key])
            if not a.IsOnLayer(la) or not z.IsOnLayer(la):return dict(status='pair_requires_layer_transition')
            terminals[net]=(xy(a.GetPosition()),xy(z.GetPosition()))
        def clear(net,a,z,width):return oracle.clear(net,la,a,z,width,pair=pair)
        def envelope(a,z,width):return oracle.clear(p,la,a,z,width,ignore_nets=(p,n)) and reference_validator.center(tuple(a),tuple(z),width+pair['gap_mm'])
        result=dict(status='joint_source_via_requested') if (stage==0 and joint_source_port) else solve_pair(p,n,terminals,bounds,clear,envelope,pair['width_mm'],pair['gap_mm'],pair['skew_mm'] if stage==len(endpoints)-2 else 1e9,pitch=pitch,max_expansions=30000,max_attempts=128,max_uncoupled=pair.get('max_uncoupled_mm',2),offsets=accumulated,accept_paths=lambda paths:reference_validator(paths,pair.get('max_uncoupled_mm',2)))
        if result['status']!='routed':
            surface_failure=result
            reuse=(joint_source_port if stage==0 else None);bridge_offsets=accumulated
            if metrics and metrics[-1].get('bridge_target'):
                old=metrics[-1]['bridge_target']
                reuse=dict(sites=old['sites'],paths={net:[] for net in (p,n)},lengths={net:0 for net in (p,n)},reuse=True)
                # The prior surface leg becomes an ESD stub, not a round trip
                # in the connector-to-receiver timing path.
                bridge_offsets={net:accumulated[net]-old['lengths'][net]-rules['electrical_fab']['board_thickness_mm'] for net in (p,n)}
            result=pair_layer_bridge(b,pair,terminals,rules,oracle,bounds,pitch,bridge_offsets,reuse_source=reuse,reference_validator=reference_validator,prior_reference=[m["reference_paths"] for m in metrics],prior_vias=vias,solution_index=topology.get("target_port_rank",0) if stage==0 else 0,max_expansions=60000 if stage and takeoff=='bridge_join_via' else 15000,timing_target_mm=topology.get("prefix_timing_target_mm",0) if stage==0 else 0)
            if result['status']!='routed':return dict(result,failed_stage=stage,terminals=terminals,surface_failure=surface_failure,planned_tracks=tracks,joint_source_port=joint_source_port)
        if result.get('pair_tracks'):
            metrics.append(result);accumulated=dict(result['lengths'])
            tracks.extend(result['pair_tracks']);vias.extend(result['pair_vias'])
            for net,layer,x,y,w in result['pair_tracks']:oracle.reserve_track(net,layer,x,y,w)
            for net,point in result['pair_vias']:oracle.reserve_via(net,point,result['via_diameter_mm'],result['via_drill_mm'])
            continue
        from pnr.route.detail.coupled import trim_path
        cap=pair.get('max_uncoupled_mm',2)
        result['reference_paths']={net:trim_path(path,cap,cap) for net,path in result['paths'].items()}
        metrics.append(result)
        accumulated=dict(result['lengths'])
        for net,path in result['paths'].items():
            paths[net]+=path
            for a,z in zip(path,path[1:]):
                tracks.append((net,la,a,z,pair['width_mm']))
                oracle.reserve_track(net,la,a,z,pair['width_mm'])
    used={x[key] for x in endpoints for key in ('p','n')}
    used|={t[side][key] for t in pair.get('auxiliary_pairs',[]) for side in ('source','target') for key in ('p','n')}
    if any(label not in used for label in bylabel):return dict(status='pair_unassigned_terminals')
    # Exact polygon containment over the coupled trunk. Fanouts have the
    # explicit bounded uncoupled allowance; impedance needs a real fab stackup.
    reference=b.GetLayerID(pair.get('reference_layer','In1.Cu'))
    plane_nets={n for c in rules.get('net_classes',[]) if c.get('plane_layer')==pair.get('reference_layer','In1.Cu') for n in c['nets']}
    fill=k.SHAPE_POLY_SET()
    for zone in b.Zones():
        if not zone.GetIsRuleArea() and zone.IsOnLayer(reference) and zone.GetNetname() in plane_nets:fill.BooleanAdd(zone.GetFilledPolysList(reference))
    for segment in metrics:
        # Inspect the actual tuned copper, not just the untuned centerline.
        # Only the explicitly bounded terminal fanouts may leave the reference.
        for path in segment['reference_paths'].values():
            for a,z in zip(path,path[1:]):
                probe=k.PCB_TRACK(b);probe.SetLayer(k.F_Cu);probe.SetStart(vec(a));probe.SetEnd(vec(z));probe.SetWidth(round((pair['width_mm']+pair['gap_mm'])*1e6))
                copper=k.SHAPE_POLY_SET();probe.TransformShapeToPolygon(copper,k.F_Cu,0,1000,k.ERROR_OUTSIDE)
                copper.BooleanSubtract(fill)
                if not copper.IsEmpty():return dict(status='pair_reference_plane_discontinuity')
    # Endpoint graph lengths exclude ESD side branches and duplicate copper.
    endpoint_metrics={}
    for net,key in ((p,'p'),(n,'n')):
        first,last=bylabel[endpoints[0][key]],bylabel[endpoints[-1][key]]
        endpoint_metrics[net]=path_metrics([(la,a,z) for nn,la,a,z,w in tracks if nn==net],[(pt,[k.F_Cu,k.B_Cu]) for nn,pt in vias if nn==net],(xy(first.GetPosition()),k.F_Cu),(xy(last.GetPosition()),k.F_Cu),layer_heights={k.F_Cu:0,k.B_Cu:rules['electrical_fab']['board_thickness_mm']})
    if not all(v.get('valid') for v in endpoint_metrics.values()):return dict(status='pair_endpoint_graph_invalid',endpoint_metrics=endpoint_metrics)
    if abs(endpoint_metrics[p]['length_mm']-endpoint_metrics[n]['length_mm'])>pair['skew_mm']+1e-6:return dict(status='pair_endpoint_skew',endpoint_metrics=endpoint_metrics)
    duplicate_metrics=duplicate_endpoint_metrics(pair,bylabel,tracks,vias,rules['electrical_fab']['board_thickness_mm'])
    if not connector_origins_qualified(pair,duplicate_metrics):return dict(status='pair_duplicate_endpoint_skew',endpoint_metrics=endpoint_metrics,connector_endpoint_metrics=duplicate_metrics)
    return dict(status='routed',pair_tracks=tracks,pair_vias=vias,segments=metrics,auxiliary=auxiliary,topology=topology,joint_source_port=joint_source_port,endpoint_metrics=endpoint_metrics,connector_endpoint_metrics=duplicate_metrics,mode='pair',via_diameter_mm=diameter,via_drill_mm=drill,impedance_qualified=False)

def screen_pair_placements(board_path,rules,pair,proposals,bounds):
    """Order legal placement proposals by exact paired escape availability.

    This is a routing heuristic, never an acceptance or placement-DRC result.
    Keep zero-via-port candidates last: a surface-only route can still work.
    """
    import pcbnew as k
    result=[]
    for proposal in proposals:
        b=k.LoadBoard(str(board_path));removed=[t for t in b.GetTracks() if t.GetNetname() in (pair['p'],pair['n'])]
        if any(t.IsLocked() for t in removed):raise ValueError('pair copper locked')
        for t in removed:b.Remove(t)
        b.BuildConnectivity()
        try:move_pair_support(b,pair,proposal)
        except ValueError:continue
        labels={f.GetReference()+'.'+p.GetNumber():xy(p.GetPosition()) for f in b.GetFootprints() for p in f.Pads()}
        chain=pair['terminal_chain'];index=next(i for i,t in enumerate(chain) if any(v.rsplit('.',1)[0]==proposal['ref'] for v in t.values()))
        source=chain[index-1]
        if index==1:
            for aux in pair.get('auxiliary_pairs',[]):
                if aux['target']==source:source=aux['source']
        terminals={pair[key]:(labels[source[key]],labels[chain[index][key]]) for key in ('p','n')}
        ports=pair_bridge_ports(pair,terminals,rules,Oracle(b,rules),bounds,1)
        result.append(dict(proposal,paired_via_ports=len(ports)))
    return sorted(result,key=lambda p:(not bool(p['paired_via_ports']),p['score']))

if __name__=='__main__':
 from pnr.profile import run
 run('native-electrical',main)
