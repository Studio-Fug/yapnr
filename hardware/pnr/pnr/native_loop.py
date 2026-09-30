"""Checkpoint-native placement/routing feedback with auditable termination.

Controller runs under the PnR Python; workers under KiCad Python. Native boards,
not internal router estimates, are the acceptance authority. Placement moves
are local translations with full-width terminal tethers, never a global ripup.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from pnr.placement_trials import diverse_pair_poses


def open_net_coverage(report, item_nets):
    """Count exact current native opens without reusing initial coverage.

    Pad/item UUIDs are authoritative while present. Newly routed track UUIDs may
    not exist in the last inventory; match their native description only against
    known net names. Conflicting or unresolved identities retain an unknown
    bucket so counts can never silently disappear.
    """
    known = {name for name in item_nets.values() if name}
    coverage = Counter()
    for finding in report['unconnected_items']:
        names = set()
        for item in finding.get('items', []):
            name = item_nets.get(item.get('uuid'))
            if name:
                names.add(name)
            else:
                description = item.get('description', '')
                names.update(n for n in known if '[' + n + ']' in description)
        coverage[next(iter(names)) if len(names) == 1 else 'unknown'] += 1
    return coverage


def read(p):
    return json.loads(Path(p).read_text())


def save(p, value):
    Path(p).write_text(json.dumps(value, indent=2) + '\n')


def copy_lib_table(source, folder):
    """Give a board written into folder its source project's footprint table.

    Without it native DRC reports every footprint as lib_footprint_issues and
    gate() rejects the board. Returns whether source had a table to copy.
    """
    table = source.parent / 'fp-lib-table'
    if not table.exists():
        return False
    (folder / 'fp-lib-table').write_text(table.read_text().replace('${KIPRJMOD}', str(source.parent.resolve())))
    return True


def copy_board(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    for ext in ['.kicad_pcb', '.kicad_pro']:
        shutil.copyfile(source.with_suffix(ext), target.with_suffix(ext))
    # Fab-profile custom rules travel with the project (native DRC rewrites them
    # anyway; copying keeps warm/interactive sessions on the same policy).
    if source.with_suffix('.kicad_dru').exists():
        shutil.copyfile(source.with_suffix('.kicad_dru'), target.with_suffix('.kicad_dru'))
    copy_lib_table(source, target.parent)


# Labels passed to phase(); PNR_STOP_AFTER_PHASE must name one of them.
PHASE_LABELS = ('02-usb-pairs', '03-early-power', '04-early-plane', '05-early-power-refine', '06-signals',
                '06b-power-bank-consolidation', '07-native-refinement', '08b-power-bank-consolidation')


class StopAfterPhase(Exception):
    """PNR_STOP_AFTER_PHASE reached; result is the loop's written best board."""
    def __init__(self, label, result):
        super().__init__(label)
        self.label, self.result = label, result


def native_worker(a):
    import pcbnew as k
    from pnr.ingest import build_graph
    from pnr.via_coalesce import partition, preserved, protected, touch
    from pnr.pad_entry import snapshot
    from pnr.plane_access import uid
    from pnr.fab_profile import load_board  # custom rules in force for the 'check' refill
    b = load_board(a.board); b.BuildConnectivity()
    rules = read(a.rules)
    excluded, intents = protected(b, rules, a.annotation_source)
    movement_excluded=set(excluded)
    if rules.get('electrical_fab'):
        # Width-aware terminal replacement can move power-support parts. Keep
        # explicit arrays/returns and coupled pair terminals protected until an
        # atomic pair-placement transaction can validate their full topology.
        movement_excluded={n for pair in rules.get('diff_pairs',[]) for n in (pair['p'],pair['n'])}
        movement_excluded.update(n for group in rules.get('length_match',[]) for n in group['nets'])
    pads = [p for f in b.GetFootprints() for p in f.Pads()]
    tracks = list(b.GetTracks())
    xy = lambda p: [p.x / 1e6, p.y / 1e6]
    if a.worker == 'prepare':
        from pnr.electrical import annotations, resolve_currents, compile_policy, resolve_pair_chains
        g = build_graph(b)
        resolved = resolve_currents(annotations(a.annotation_source), g.components)
        from pnr.fab_profile import apply_fab_model, apply_rules
        compiled = compile_policy(rules, resolved, apply_fab_model(read(a.electrical_fab)))
        compiled = resolve_pair_chains(compiled, a.annotation_source, g.components)
        compiled = apply_rules(compiled)
        from pnr.si import enabled as si_enabled
        if si_enabled():
            # PNR_SI=1: resolve @pnr-si bindings here (graph + pcbnew); the parent runs
            # the design-time check (pnr.si.report.gate_rules_file) in the pnr runtime.
            from pnr.si.annotations import AnnotationError
            from pnr.si.extract import estimate_geometries
            from pnr.si.report import resolve_intents
            try:
                intents = resolve_intents(a.annotation_source, g.components)
            except AnnotationError as error:
                sys.exit('pnr.si: @pnr-si annotation error: %s' % error)
            compiled = dict(compiled, si_intents=intents)
            from pnr.si.physics import stackup
            save(a.report.parent / 'si-estimate.json', estimate_geometries(intents, g.components, st=stackup(compiled)))
        save(a.report, compiled)
        return
    if a.worker == 'terminal-blockers':
        from pnr.terminal_repair import suggest
        save(a.report,suggest(b,rules,read(a.spec),excluded))
        return
    if a.worker == 'inspect':
        groups = partition(b)
        membership = {p: i for i, g in enumerate(groups) for p in g}
        from pnr.connectivity_restore import restoration_membership, restores_connection
        restoration = restoration_membership(rules.get('restoration_partition', []), groups)
        bynet = defaultdict(list)
        for p in pads:
            if p.GetNetCode() and (rules.get('electrical_fab') or p.GetNetname() not in excluded):
                bynet[p.GetNetname()].append(p)
        targets = []
        shove=os.environ.get('PNR_SHOVE')=='1'
        for net, ps in bynet.items():
            choices = []
            for i, p in enumerate(ps):
                for q in ps[i+1:]:
                    if membership[uid(p)] != membership[uid(q)]:
                        choices.append((math.dist(xy(p.GetPosition()), xy(q.GetPosition())), uid(p), uid(q), p, q))
            # Cover every disconnected component, not just the nearest pair
            # on a net. Repeatedly retrying one trapped pad starves easier opens.
            roots={membership[uid(p)]:membership[uid(p)] for p in ps}
            def root(i):
                while roots[i]!=i:i=roots[i]
                return i
            ordered=sorted(choices, key=lambda c: (not restores_connection(restoration,c[1],c[2]),c[:3]))
            trunk_ids=[]
            if shove and rules.get('electrical_fab'):
                # PNR_SHOVE=1 (A2): the full-current trunk (highest terminal contract,
                # via a bulk capacitor hop, to the net-scope carrier) is targeted first,
                # so leaf branches later attach to it instead of racing it.
                from pnr.shove.targets import trunk_pairs
                trunk_ids=[{uid(x),uid(y)} for x,y in trunk_pairs(net,ps,rules,lambda t:membership[uid(t)])]
                if trunk_ids:
                    ordered=[c for ids in trunk_ids for c in ordered if {c[1],c[2]}==ids]+[c for c in ordered if {c[1],c[2]} not in trunk_ids]
            for distance, _, _, p, q in ordered:
                x,y=root(membership[uid(p)]),root(membership[uid(q)])
                if x==y:continue
                roots[x]=y
                label = lambda t: t.GetParentFootprint().GetReference()+'.'+t.GetNumber()
                from pnr.electrical import net_policy
                policy=net_policy(net,rules)
                related=[]
                if policy['mode']=='pair':
                    related=sorted({v.rsplit('.',1)[0] for t in policy['pair'].get('terminal_chain',[]) for v in t.values()})
                targets.append(dict(restoration_required=restores_connection(restoration,uid(p),uid(q)), net=net, source=label(p), target=label(q), source_uuid=uid(p), target_uuid=uid(q), source_xy=xy(p.GetPosition()), target_xy=xy(q.GetPosition()), distance=distance, mode=policy['mode'], pair_name=policy.get('pair',{}).get('name'), related_refs=related))
                if shove and policy['mode'] in ('power','plane'):
                    # A3: the root copper a branch may attach to (both groups' pads and
                    # copper), so the search box can include it.
                    from pnr.shove.targets import group_box
                    targets[-1]['root_box']=group_box(b,[membership[uid(p)],membership[uid(q)]],groups,net)
                    if {uid(p),uid(q)} in trunk_ids:targets[-1]['trunk']=True
                    from pnr.shove.targets import leaf_current
                    leaf=leaf_current(net,[p,q],rules)
                    if leaf is not None:targets[-1]['leaf_rms_a']=leaf
        g = build_graph(b)
        physical_locks = sorted(c.ref for c in g.components if c.locked)
        for c in g.components:
            # Protect non-plane power/pair pad geometry and explicit source contracts.
            plane_nets = {n for cl in rules.get('net_classes', []) if cl.get('plane_layer') for n in cl.get('nets', [])}
            if any(p.net in movement_excluded-plane_nets for p in c.pads) or any(i['ref']==c.ref for i in intents):
                c.locked = True
        owners = {uid(p): p.GetParentFootprint().GetReference() for p in pads}
        # Copper blocker attribution to nearest same-net pad is a heuristic only.
        for t in tracks:
            ps = [p for p in pads if p.GetNetCode()==t.GetNetCode()]
            if ps:
                p = min(ps, key=lambda p: math.dist(xy(p.GetPosition()), xy(t.GetStart())))
                owners[uid(t)] = p.GetParentFootprint().GetReference()
        from pnr.ingest import _board_frame
        frame,_=_board_frame(b)
        point=lambda p:frame.point(p.x,p.y)
        routing_geometry=dict(tracks=[(t.GetNetname(),t.GetLayerName(),point(t.GetStart()),point(t.GetEnd()),t.GetWidth()/1e6) for t in tracks if t.GetClass()=='PCB_TRACK'],vias=[(t.GetNetname(),*point(t.GetPosition())) for t in tracks if t.GetClass()=='PCB_VIA'],unsupported=[t.GetClass() for t in tracks if t.GetClass() not in ('PCB_TRACK','PCB_VIA')])
        save(a.report, dict(routing_geometry=routing_geometry,graph=json.loads(g.to_json()), physical_locks=physical_locks, targets=sorted(targets,key=lambda t:(t['distance'],t['net'])), item_nets={uid(t):t.GetNetname() for t in pads+tracks}, item_kinds={uid(t):t.GetClass() for t in pads+tracks}, item_layers={uid(t):t.GetLayerName() for t in tracks}, owners=owners, bounds=[b.GetBoardEdgesBoundingBox().GetLeft()/1e6,b.GetBoardEdgesBoundingBox().GetTop()/1e6,b.GetBoardEdgesBoundingBox().GetRight()/1e6,b.GetBoardEdgesBoundingBox().GetBottom()/1e6], excluded=sorted(excluded), partition=groups, entries=snapshot(b,rules), footprint_poses={f.GetReference():xy(f.GetPosition()) for f in b.GetFootprints()}))
        return
    if a.worker == 'placement-copper':
        from pnr.placement_copper import rank_native_copper
        save(a.report,rank_native_copper(b,rules,read(a.spec)));return
    if a.worker == 'unmove':
        spec=read(a.spec)
        prior=k.LoadBoard(spec['before']);moved=k.LoadBoard(spec['moved'])
        old={uid(t):t for t in prior.GetTracks()};mid={uid(t):t for t in moved.GetTracks()};now={uid(t):t for t in b.GetTracks()}
        added=set(mid)-set(old);removed=set(old)-set(mid)
        def geometry(t):
            return (t.GetClass(),t.GetNetname(),t.GetLayer(),xy(t.GetStart()),xy(t.GetEnd()),t.GetWidth() if t.GetClass()=='PCB_TRACK' else t.GetWidth(k.F_Cu))
        if not added<=set(now) or removed & set(now) or any(geometry(now[i])!=geometry(mid[i]) for i in added):
            save(a.report,dict(skipped='placement copper was changed by routing'));return
        retained_wrappers=[now[i] for i in added]
        for t in retained_wrappers:b.Remove(t)
        clones=[]
        for i in removed:
            clone=old[i].Duplicate();clone.SetUuid(old[i].m_Uuid);b.Add(clone)
            # Rebind Duplicate's source-board NETINFO_ITEM to the destination.
            clone.SetNetCode(0 if old[i].GetNetCode()==0 else b.FindNet(old[i].GetNetname()).GetNetCode())
            clones.append(clone)
        original=next(f for f in prior.GetFootprints() if f.GetReference()==spec['ref'])
        current=next(f for f in b.GetFootprints() if f.GetReference()==spec['ref'])
        current.SetPosition(original.GetPosition())
        k.SaveBoard(str(a.out),b)
        save(a.report,dict(restored_ref=spec['ref'],removed_tethers=len(added),restored_tracks=len(removed)))
        return
    if a.worker == 'move':
        from pnr.route.detail.keyhole import elbows
        spec = read(a.spec)
        f = next(f for f in b.GetFootprints() if f.GetReference()==spec['ref'])
        plane_nets = {n for cl in rules.get('net_classes', []) if cl.get('plane_layer') for n in cl.get('nets', [])}
        if f.IsLocked() or any(p.GetNetname() in movement_excluded-plane_nets for p in f.Pads()) or any(i['ref']==f.GetReference() for i in intents):
            raise ValueError('locked or source-protected component')
        delta = k.VECTOR2I(round(spec['dx']*1e6), round(spec['dy']*1e6))
        # Replace local terminal segments instead of retaining a disconnected
        # fanout under the translated pad field. Whole-track deletion is guarded
        # by the later original-pad partition check (including interior branches).
        old=[]
        for p in f.Pads():
            contacts=[t for t in tracks if any(touch(t,p,la) for la in b.GetEnabledLayers().CuStack())]
            old.append((p,p.GetPosition(),contacts,p.GetBoundingBox()))
        f.SetPosition(f.GetPosition()+delta)
        added=[];removed=[];handled=set()
        for p,origin,contacts,box in old:
            if not contacts or not p.GetNetCode():continue
            if p.GetNetname() in movement_excluded-plane_nets:continue
            for contact in contacts:
                if uid(contact) in handled:continue
                layers=[la for la in (k.F_Cu,k.In2_Cu,k.B_Cu) if p.IsOnLayer(la) and contact.IsOnLayer(la)]
                if not layers:continue
                la=layers[0];anchor=xy(origin)
                width=round(rules.get('fab',{}).get('track_width_mm',.2)*1e6)
                if contact.GetClass()=='PCB_TRACK':
                    # A segment between pads of this footprint moves with BOTH
                    # endpoints. Replacing it once with an old endpoint anchor
                    # strands the second pad when handled suppresses its repair.
                    start_local=any(q.GetNetCode()==contact.GetNetCode() and q.IsOnLayer(la) and qb.Contains(contact.GetStart()) for q,qo,qc,qb in old)
                    end_local=any(q.GetNetCode()==contact.GetNetCode() and q.IsOnLayer(la) and qb.Contains(contact.GetEnd()) for q,qo,qc,qb in old)
                    if start_local and end_local and not contact.IsLocked():
                        clone=contact.Duplicate();clone.SetStart(contact.GetStart()+delta);clone.SetEnd(contact.GetEnd()+delta)
                        b.Remove(contact);removed.append(contact);handled.add(uid(contact));b.Add(clone);added.append(clone)
                        continue
                    width=contact.GetWidth()
                    at_start=box.Contains(contact.GetStart());at_end=box.Contains(contact.GetEnd())
                    if not contact.IsLocked() and at_start != at_end:
                        anchor=xy(contact.GetEnd() if at_start else contact.GetStart())
                        b.Remove(contact);removed.append(contact);handled.add(uid(contact))
                if contact.GetClass()=='PCB_VIA':anchor=xy(contact.GetPosition())
                if rules.get('electrical_fab'):
                    from pnr.pad_entry import required_width
                    width=max(width,round(required_width(p,rules)*1e6))
                path=list(elbows(anchor,xy(p.GetPosition())))[spec.get('elbow',0)]
                for start,end in zip(path,path[1:]):
                    if start==end:continue
                    t=k.PCB_TRACK(b);t.SetNetCode(p.GetNetCode());t.SetLayer(la);t.SetWidth(width)
                    t.SetStart(k.VECTOR2I(*[round(v*1e6) for v in start]));t.SetEnd(k.VECTOR2I(*[round(v*1e6) for v in end]));b.Add(t);added.append(t)
        k.SaveBoard(str(a.out),b)
        save(a.report,dict(move=spec,added_tracks=len(added),removed_tracks=[uid(t) for t in removed]))
        return
    if a.worker == 'check':
        k.ZONE_FILLER(b).Fill(b.Zones());b.BuildConnectivity()
        before=read(a.spec); entries=snapshot(b,rules)
        groups_now=partition(b)
        result=dict(preserved=preserved(before['partition'],groups_now), lost_pad_entries=[i for i,v in before['entries'].items() if v and not entries.get(i,False)])
        from pnr.connectivity_restore import lost_connections
        result['lost_connections']=lost_connections(before['partition'],groups_now)
        from pnr.native_electrical import reference_failures
        result['reference_failures']=reference_failures(b,rules)
        k.SaveBoard(str(a.board),b);save(a.report,result)


def unique_route_jobs(targets):
    """One complete coupled-chain solve per pair per sweep, retaining all other jobs."""
    seen=set();jobs=[]
    for target in targets:
        pair=target.get('pair_name') if target.get('mode')=='pair' else None
        if pair:
            if pair in seen:continue
            seen.add(pair)
        jobs.append(target)
    return jobs


def route_job_key(target):
    if target.get('mode') == 'pair' and target.get('pair_name'):
        return ('pair', target['pair_name'])
    ends = sorted(target.get(end + '_uuid') or target[end]
                  for end in ('source', 'target'))
    return (target['net'], *ends)


def progress_budget_hint(outcome, seconds, maximum):
    """Escalate conflict negotiation only after all independent paths exist.

    This is search scheduling evidence, not native connectivity or acceptance.
    A blocked first path and power/plane failures do not earn this hint.
    """
    if outcome.get('status') != 'time_budget':return 0
    attempts=outcome.get('attempts',[])
    if not attempts or attempts[0].get('stage')!='independent':return 0
    initial=attempts[0];events=initial.get('events',[])
    if (len(events)<2 or initial.get('completed')!=len(events) or
            any(e.get('status')!='routed' for e in events) or
            not any(a.get('stage')=='conflicts' for a in attempts)):return 0
    return min(maximum,max(20.,2*seconds))


def route_search_seconds(maximum, attempt):
    """Increase work on retries, not on late first attempts in a later cycle."""
    return min(maximum, 5 * max(1, attempt) if attempt <= 4 else 20 * 2 ** min(attempt - 4, 8))


def scheduled_route_jobs(targets, attempts):
    """Untested terminal pairs precede repeat failures; keep shortest-first ties."""
    if os.environ.get('PNR_SHOVE')=='1':
        # A2: full-current trunks first; their copper roots every later branch.
        from pnr.shove.targets import schedule_key
        return sorted(unique_route_jobs(targets),
                      key=lambda t: (schedule_key(t), attempts.get(route_job_key(t), 0),
                                     not t.get('restoration_required',False),
                                     t.get('distance', 0), route_job_key(t)))
    return sorted(unique_route_jobs(targets),
                  key=lambda t: (attempts.get(route_job_key(t), 0),
                                 not t.get('restoration_required',False),
                                 t.get('distance', 0), route_job_key(t)))


def affected_route_jobs(inventory, ref, failure_history):
    """Repair moved terminals and nets whose observed blockers belong to this part."""
    blocked_nets = {net for net, blockers in failure_history.items()
                    if any(inventory['owners'].get(item) == ref for item in blockers)}
    # Sharing a global supply with the moved part does not make every remote
    # supply connection a useful placement repair trial.
    return [t for t in inventory['targets']
            if t['net'] in blocked_nets or ref in t.get('related_refs', [])
            or any(t.get(end, '').rsplit('.', 1)[0] == ref
                   for end in ('source', 'target'))]


def diverse_placement_trials(candidates, limit):
    """Spend small trial budgets on distinct blockers before another pose."""
    first, later, refs = [], [], set()
    for candidate in candidates:
        if candidate['ref'] in refs:
            later.append(candidate)
        else:
            first.append(candidate)
            refs.add(candidate['ref'])
    return (first + later)[:limit]


def score_failures(history, failures, owners):
    """Persistent per-component native failure pressure, with explicit attribution."""
    scores=dict(history)
    for f in failures:
        target=f['target']
        for key in ['source','target']:
            ref=target[key].rsplit('.',1)[0];scores[ref]=scores.get(ref,0)+1
        for ref in target.get('related_refs',[]):scores[ref]=scores.get(ref,0)+1
        blockers=f.get('static_blockers',{})
        total=sum(blockers.values()) or 1
        for identity,n in blockers.items():
            ref=owners.get(identity)
            if ref: scores[ref]=scores.get(ref,0)+n/total
    return scores


def rank_translation_channels(graph,rules,candidates):
    """Rank legal poses by surface escape deficit before spending native trials.

    This ignores routed copper: the transaction's native guards remain mandatory.
    Only pose order changes; persistent blocker scores still choose components.
    """
    import numpy as np
    from pnr.place.channels import ChannelModel
    model=ChannelModel(graph,rules);result=[]
    for comp in graph.components:
        poses=[dict(p) for p in candidates if p['ref']==comp.ref]
        if not poses:continue
        others=[c for c in graph.components if c.ref!=comp.ref]
        baseline=float(model.penalty(comp,others,*comp.pos))
        xs=np.array([comp.pos[0]+p['dx'] for p in poses])
        ys=np.array([comp.pos[1]-p['dy'] for p in poses])
        scores=model.penalty(comp,others,xs,ys)
        for pose,score in zip(poses,scores):
            pose['channel_penalty']=float(score)
            pose['channel_improvement']=baseline-float(score)
        poses.sort(key=lambda p:(p['channel_penalty'],math.hypot(p['dx'],p['dy']),p['rank']))
        for rank,pose in enumerate(poses):pose['rank']=rank
        result.extend(poses)
    return result


def placements(inventory, constraints_path, scores, tried, original, max_move, rules=None):
    import yaml
    from pnr.graph import BoardGraph
    from pnr.constraints import compile_constraints
    from pnr.place.geometry import resolve_fixed_poses
    from pnr.place.metrics import hard_violations,translation_checker
    g=BoardGraph.from_json(json.dumps(inventory['graph']))
    cc=compile_constraints(yaml.safe_load(Path(constraints_path).read_text()),g.refs,{c.address:c.ref for c in g.components},{f'{c.address}:{p.name}':p.net for c in g.components for p in c.pads})
    fixed=resolve_fixed_poses(g,cc)
    holes={h['name'] for h in cc.mounting_holes}
    g.components=[c for c in g.components if c.ref not in holes]
    base=hard_violations(g,cc)
    if any(base.values()):
        raise ValueError('baseline hard placement violations: '+json.dumps(base))
    legal=translation_checker(g,cc)
    result=[]
    for c in sorted(g.components,key=lambda c:(-scores.get(c.ref,0),c.ref)):
        if c.locked or c.ref in fixed or not scores.get(c.ref):continue
        old=tuple(c.pos)
        for step in [.25,.5,1.0]:
            for dx,dy in [(0,-step),(step,0),(0,step),(-step,0),(step,-step),(-step,-step),(step,step),(-step,step)]:
                # Engine y up; native y down. Bounds are against the ORIGINAL input,
                # so successive accepted moves cannot creep beyond the configured cap.
                native=[inventory['footprint_poses'][c.ref][0]+dx,inventory['footprint_poses'][c.ref][1]+dy]
                identity=(c.ref,round(native[0],6),round(native[1],6))
                if identity in tried or math.dist(native,original[c.ref])>max_move+1e-9:continue
                c.pos=(old[0]+dx,old[1]-dy)
                if legal(c):
                    result.append(dict(ref=c.ref,dx=dx,dy=dy,position=native,score=scores[c.ref],rank=sum(o['ref']==c.ref for o in result)))
                c.pos=old
    if rules:
        result=rank_translation_channels(g,rules,result)
    # Interleave components: one congested IC cannot consume every trial.
    return sorted(result,key=lambda o:(o['rank'],-o['score'],o['ref']))



def pair_placements(inventory,constraints_path,pair):
    """Legal source-topology proposals near the connector for intermediate ICs.

    This is a separate atomic coupled transaction, not a sequence of accepted
    1-mm moves dragging long USB traces across the board.
    """
    import yaml
    from pnr.graph import BoardGraph
    from pnr.constraints import compile_constraints
    from pnr.place.geometry import resolve_fixed_poses
    from pnr.place.metrics import hard_violations
    g=BoardGraph.from_json(json.dumps(inventory['graph']))
    # Generic movement protects all pair terminals. Atomic paired placement may
    # move source-declared intermediate devices, while respecting actual locks.
    if 'physical_locks' in inventory:
        for c in g.components:c.locked=c.ref in inventory['physical_locks']
    cc=compile_constraints(yaml.safe_load(Path(constraints_path).read_text()),g.refs,{c.address:c.ref for c in g.components},{f'{c.address}:{p.name}':p.net for c in g.components for p in c.pads})
    fixed=resolve_fixed_poses(g,cc);holes={h['name'] for h in cc.mounting_holes}
    g.components=[c for c in g.components if c.ref not in holes]
    chain=pair.get('terminal_chain',[])
    if len(chain)<3:return []
    source_ref=chain[0]['p'].rsplit('.',1)[0]
    from pnr.place.geometry import pin_positions
    byref={c.ref:c for c in g.components}
    effective_source=chain[0]
    for aux in pair.get('auxiliary_pairs',[]):
        if aux['target']==effective_source:effective_source=aux['source']
    def terminal(label):
        ref,num=label.rsplit('.',1)
        points=[point for name,point in pin_positions(byref[ref]) if name==num]
        if len(points)!=1:raise ValueError('ambiguous pair placement endpoint '+label)
        return points[0]
    def center(spec):
        points=[terminal(spec[key]) for key in ('p','n')]
        return tuple(sum(p[i] for p in points)/2 for i in (0,1))
    source_center=center(effective_source)
    target_center=center(chain[-1])
    axis=tuple(target_center[i]-source_center[i] for i in (0,1));norm=math.hypot(*axis)
    if norm<1e-8:return []
    axis=tuple(v/norm for v in axis);normal=(-axis[1],axis[0])
    refs={v.rsplit('.',1)[0] for t in chain[1:-1] for v in t.values()}
    result=[]
    for c in g.components:
        if c.ref not in refs or c.ref in fixed or c.locked:continue
        original=inventory['footprint_poses'][c.ref];old=tuple(c.pos);old_rot=c.rot
        node=next(t for t in chain[1:-1] if all(v.rsplit('.',1)[0]==c.ref for v in t.values()))
        # Search the inward source corridor in half-mm steps, including all
        # cardinal orientations. No reference names or board coordinates.
        for distance in (2,2.5,3,3.5,4,4.5,5,5.5,6):
            for lateral in (0,-.5,.5,-1,1,-2,2):
                c.pos=tuple(source_center[i]+axis[i]*distance+normal[i]*lateral for i in (0,1))
                for rotation in (0,90,180,270):
                    c.rot=rotation
                    if any(hard_violations(g,cc).values()):continue
                    points={key:terminal(node[key]) for key in ('p','n')}
                    lead=sum(math.dist(terminal(effective_source[key]),points[key]) for key in ('p','n'))
                    downstream=sum(math.dist(points[key],terminal(chain[-1][key])) for key in ('p','n'))
                    # Short connector branches are primary; align polarities
                    # to avoid a forced crossover directly outside the package.
                    sep=tuple(points['p'][i]-points['n'][i] for i in (0,1))
                    origin=tuple(terminal(effective_source['p'])[i]-terminal(effective_source['n'])[i] for i in (0,1))
                    alignment=sum(sep[i]*origin[i] for i in (0,1))/(math.hypot(*sep)*math.hypot(*origin))
                    score=lead+.1*downstream+2*(1-alignment)
                    pos=[original[0]+c.pos[0]-old[0],original[1]-c.pos[1]+old[1]]
                    result.append(dict(ref=c.ref,original=original,position=pos,original_rotation=old_rot,rotation=rotation,score=score))
        c.pos=old;c.rot=old_rot
    return sorted(result,key=lambda p:(p['score'],p['ref'],p['rotation'],p['position']))

def repair_strategies_exhausted(targets, attempts, only_mode=None, maximum_seconds=20):
    """A no-move stop must exhaust each configured grid/refinement strategy."""
    return all(attempts.get(route_job_key(t), 0) >= 4
               and route_search_seconds(maximum_seconds, attempts.get(route_job_key(t), 0)) >= maximum_seconds
               for t in unique_route_jobs(targets)
               if not only_mode or t.get('mode', 'signal') == only_mode)


def terminal_repair_nets(worker,board,route_dir,target):
    # The regional adapter owns route_dir and requires it not to exist yet.
    folder=route_dir.parent/(route_dir.name+'-terminal-analysis')
    folder.mkdir(parents=True,exist_ok=False)
    spec=folder/'target.json';save(spec,target)
    return worker('terminal-blockers',board,folder/'probe',['--spec',str(spec)])['reopen']


def electrical_search_bounds(mode, attempt, local, board, enabled=False):
    # Local retries can exclude a legal full-current path around dense packages.
    # Widen only later power attempts; the oracle still enforces exact edges,
    # copper/current constraints, pad entries and whole-board native acceptance.
    if mode == 'pair' or (enabled and mode == 'power' and attempt >= 3):
        return list(board)
    return list(local)


def route_search_pitch(attempt):
    """Spend initial retries on broad channels before the finest pad grid.

    Every segment and off-grid access still uses exact native clearance. This
    changes search discretization only, never terminal geometry or track width.
    """
    return (.25,.15,.1,.05)[min(3,max(0,attempt-1))]


def gate(before, after, checks, strict=True):
    from pnr.via_coalesce import acceptable
    return bool(not checks.get("reference_failures") and acceptable(before,after,checks) and (not strict or len(after['unconnected_items'])<len(before['unconnected_items'])))


def main(argv=None,nested=False):
    """Controller/worker entry. PNR_STOP_AFTER_PHASE returns its stopped best board."""
    try:return controller(argv,nested)
    except StopAfterPhase as stop:return stop.result


def controller(argv=None,nested=False):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('board',type=Path);ap.add_argument('--rules',required=True,type=Path)
    ap.add_argument('--annotation-source',action='append',default=[],type=Path)
    ap.add_argument('--early-pairs',action='store_true')
    ap.add_argument('--early-pair-placement',action='store_true',help='Allow source-legal pair-package rescue even when later refinement is route-only')
    ap.add_argument('--phase-dir',type=Path)
    ap.add_argument('--route-only',action='store_true')
    ap.add_argument('--only-mode',choices=['signal','power','plane','pair'])
    ap.add_argument('--electrical-fab',type=Path)
    ap.add_argument('--constraints',type=Path);ap.add_argument('--out-dir',type=Path)
    ap.add_argument('--kicad-python');ap.add_argument('--kicad-cli')
    ap.add_argument('--repo',type=Path,default=Path.cwd())
    ap.add_argument('--cycles',type=int,default=3);ap.add_argument('--route-attempts',type=int,default=10)
    ap.add_argument('--placement-attempts',type=int,default=8);ap.add_argument('--max-move',type=float,default=2)
    ap.add_argument('--search-seconds',type=float,default=20);ap.add_argument('--seconds',type=float,default=1200)
    ap.add_argument('--worker',choices=['prepare','inspect','move','unmove','check','terminal-blockers','placement-copper']);ap.add_argument('--report',type=Path);ap.add_argument('--spec',type=Path);ap.add_argument('--out',type=Path)
    ap.add_argument('--shove-origin-board',type=Path,help='PNR_SHOVE=1: the flow origin placement (make-room nudges are capped against it); nested phases get the outer baseline')
    a=ap.parse_args(argv)
    from pnr.live import emit
    if a.worker:return native_worker(a)
    a.repo=a.repo.resolve();os.chdir(a.repo)
    if not all([a.constraints,a.out_dir,a.kicad_python,a.kicad_cli]):ap.error('controller requires constraints, out-dir and KiCad runtimes')
    if min(a.cycles,a.route_attempts,a.placement_attempts,a.max_move,a.search_seconds,a.seconds)<=0:ap.error('positive budgets required')
    # Recursive early subphases share os.environ; only the outer loop stops.
    stop_label=None if nested else os.environ.get('PNR_STOP_AFTER_PHASE') or None
    if stop_label and stop_label not in PHASE_LABELS:ap.error('PNR_STOP_AFTER_PHASE must be one of: '+', '.join(PHASE_LABELS))
    a.out_dir=a.out_dir.resolve();a.out_dir.mkdir(parents=True,exist_ok=False)
    worker_root=a.out_dir/'worker-source'
    shutil.copytree(Path(__file__).resolve().parent,worker_root/'hardware/pnr/pnr',ignore=shutil.ignore_patterns('__pycache__'))
    from pnr.si import enabled as si_enabled
    if si_enabled() and (Path(__file__).resolve().parents[1]/'si_models').is_dir():
        # PNR_SI=1: the prepare worker resolves @pnr-si parts against the committed models.
        shutil.copytree(Path(__file__).resolve().parents[1]/'si_models',worker_root/'hardware/pnr/si_models')
    (worker_root/'hardware/tools').mkdir(parents=True)
    shutil.copyfile(a.repo/'hardware/tools/keyhole_region.py',worker_root/'hardware/tools/keyhole_region.py')
    inputs=a.out_dir/'source-inputs';inputs.mkdir()
    origins=[];sources=[]
    for index,source in enumerate(a.annotation_source):
        target=inputs/(str(index)+'-'+source.name);shutil.copyfile(source,target)
        origins.append(dict(original=str(source.resolve()),snapshot=str(target),sha256=hashlib.sha256(target.read_bytes()).hexdigest()));sources.append(target)
    a.annotation_source=sources
    save(inputs/'origins.json',origins)
    # Standalone runs may pass pre-profile rules: workers get the profiled copy.
    from pnr.fab_profile import apply_rules
    source_rules=read(a.rules);profiled=apply_rules(source_rules)
    if profiled is not source_rules:a.rules=inputs/'rules.fab-profile.json';save(a.rules,profiled)
    env=dict(os.environ,PYTHONPATH=str(worker_root/'hardware/pnr'))
    from pnr.phase_budget import PhaseClock
    phase_clock=PhaseClock();started=phase_clock.started;events=[];rounds=[];history={};tried=set();seq=0;failure_history={};job_attempts={};job_budget_hints={};electrical_repair_attempts=set()
    from pnr.plane_leaf_repair import RepairBudget
    plane_leaf_budget=RepairBudget()
    from pnr.power_detour_repair import DetourBudget
    power_detour_budget=DetourBudget()
    if os.environ.get('PNR_SHOVE')=='1':
        from pnr.shove.control import ShoveBudget
        shove_budget=ShoveBudget(3 if nested else 6)
    current=a.out_dir/'baseline.kicad_pcb';copy_board(a.board.resolve(),current)
    if os.environ.get('PNR_SHOVE')=='1':
        # Make-room nudges are capped (0.5 mm in total) against the placement the
        # whole flow started from, not each transaction's own start.
        shove_origin=(a.shove_origin_board or current).resolve()
    if profiled is not source_rules:
        # The input project may predate the profile; the DRC gate must see the same
        # board constraints the workers route with.
        from pnr.writeback import patch_project_rules
        patch_project_rules(str(current.with_suffix('.kicad_pro')),profiled)
    def invoke(cmd,log):
        with Path(log).open('w') as f:
            # KiCad's python occasionally dies by signal (seen: SIGSEGV at teardown);
            # an identical rerun succeeds, so retry once on a signal exit only.
            for attempt in (0,1):
                from pnr.proc import run as run_bounded
                code=run_bounded(cmd,env=env,stdout=f,stderr=subprocess.STDOUT)
                if code>=0 or attempt:break
                f.write('\n[retry after signal %d]\n'%-code);f.flush()
            if code:raise subprocess.CalledProcessError(code,cmd)
    def worker(mode,board,folder,extra=()):
        folder.mkdir(parents=True,exist_ok=True);report=folder/(mode+'.json')
        cmd=[a.kicad_python,'-m','pnr.native_loop',str(board),'--worker',mode,'--rules',str(a.rules.resolve()),'--report',str(report)]
        for s in a.annotation_source:cmd+=['--annotation-source',str(s.resolve())]
        invoke(cmd+list(extra),folder/(mode+'.log'));return read(report)
    def drc(board, *, final=False):
        report=board.with_suffix('.drc.json')
        from pnr.native_drc import run_drc
        return run_drc(a.kicad_cli,board,report,env=env,final=final)
    if a.electrical_fab:
        try:compiled=worker('prepare',current,a.out_dir/'policy',['--electrical-fab',str(a.electrical_fab.resolve())])
        except subprocess.CalledProcessError:
            log=a.out_dir/'policy'/'prepare.log'
            why=[l for l in (log.read_text(errors='replace').splitlines() if log.exists() else []) if l.startswith('pnr.si:')]
            if si_enabled() and why:sys.exit(why[-1])
            raise
        a.rules=a.out_dir/'policy'/'prepare.json'
        if si_enabled() and not nested:
            # PNR_SI=1 compile-time check: ideal-board decks gate (unless waived), the
            # placement estimate is report-only; writes policy/si-design.json.
            from pnr.si.report import SIDesignError, gate_rules_file
            try:gate_rules_file(a.rules,out_dir=a.out_dir/'policy',estimates=a.out_dir/'policy'/'si-estimate.json',save_waves=False)
            except SIDesignError as error:sys.exit(str(error))
    def phase(name,board,metadata=None,stop=True):
        if a.phase_dir:
            from pnr.phase_capture import capture
            capture(a.phase_dir,name,board,a.rules,a.kicad_cli,metadata=metadata)
        if stop:stop_after(name,board)
    def stop_after(name,board):
        # Opt-in diagnostic cut. Publish this phase's board with the normal
        # termination contract (best copy, final DRC, progress.json, returned
        # path) so full_iteration still coalesces, audits and scores it.
        if name!=stop_label:return
        result=a.out_dir/'best'/'candidate.kicad_pcb';board=Path(board)
        if board.resolve()!=result.resolve():copy_board(board,result)
        report=drc(result,final=True);opens=len(report['unconnected_items']);progress_path=a.out_dir/'progress.json'
        inventory=worker('inspect',result,a.out_dir/'stopped-inventory');coverage=open_net_coverage(report,inventory['item_nets'])
        progress=read(progress_path) if progress_path.exists() else dict(source=str(a.board.resolve()),source_sha256=hashlib.sha256(a.board.read_bytes()).hexdigest(),initial_opens=opens,elapsed_seconds=0.,prerequisite_seconds=round(phase_clock.now()-phase_clock.overall_started,2),electrical_modes_enabled=bool(a.electrical_fab),budgets=dict(cycles=a.cycles,route_attempts=a.route_attempts,placement_attempts=a.placement_attempts,seconds=a.seconds))
        # Post-refinement phases (07/08b) keep the loop's own reason alongside.
        if 'termination' in progress:progress.update(loop_termination=progress['termination'],elapsed_seconds=round(time.monotonic()-started,2))
        progress.update(best=str(result),best_sha256=hashlib.sha256(result.read_bytes()).hexdigest(),opens=opens,rounds=rounds,events=events,component_scores=history,native_open_nets=dict(coverage),protected_open_nets={n:c for n,c in coverage.items() if n in inventory['excluded']},termination='stopped_after_phase',stopped_after_phase=name)
        save(progress_path,progress)
        print(json.dumps(dict(best=str(result),opens=opens,cycles=len(rounds),termination='stopped_after_phase')))
        raise StopAfterPhase(name,result)
    def bank_consolidation(board, label):
        # Source-current bank geometry only; independent workers preserve pad
        # partitions, qualified entries, reference and whole-current budgets.
        # Do not multiply this pass in the recursive electrical subphases.
        if os.environ.get('PNR_POWER_BANK_REUSE')!='1' or a.only_mode or not a.electrical_fab:
            return board
        folder=a.out_dir/label;folder.mkdir()
        output=folder/'candidate.kicad_pcb'
        previous=drc(board)
        worker('inspect',board,folder/'reference')
        cmd=[a.kicad_python,'-m','pnr.power_bank_stage',str(board),
             '--rules',str(a.rules.resolve()),'--out',str(output),
             '--report',str(folder/'result.json'),'--work-dir',str(folder/'trials'),
             '--kicad-python',a.kicad_python,'--kicad-cli',a.kicad_cli,'--max-trials','4']
        event=dict(stage='power_bank_consolidation',phase=label,accepted=False,folder=str(folder))
        try:
            invoke(cmd,folder/'run.log');summary=read(folder/'result.json')
            checks=worker('check',output,folder/'checks',['--spec',str(folder/'reference/inspect.json')])
            after=drc(output)
            event.update(summary=summary,accepted=bool(summary['accepted_transactions'] and gate(previous,after,checks,strict=False)))
        except subprocess.CalledProcessError as error:
            event.update(status='worker_error',returncode=error.returncode)
        events.append(event)
        result=output if event['accepted'] else board
        phase(label,result,event)
        emit('route_result',board=result,data=dict(**event,opens=len(drc(result)['unconnected_items'])))
        return result
    def fanout(board,action,label):
        # PNR_FANOUT_RESERVE=1: named all-net rule areas keep planned fine-pitch
        # signal escapes free through phases 03-05; release deletes exactly them
        # before staged signals. Both pass the partition/pad-entry/DRC gate.
        folder=a.out_dir/('fanout-'+action);base=folder/'baseline.kicad_pcb';output=folder/'candidate.kicad_pcb'
        copy_board(board,base);copy_board(board,output);previous=drc(base);worker('inspect',base,folder/'reference')
        event=dict(stage='fanout_'+action,accepted=False,folder=str(folder))
        try:
            invoke([a.kicad_python,'-m','pnr.fanout_reserve',str(base),'--rules',str(a.rules.resolve()),'--out',str(output),
                    '--report',str(folder/'result.json')]+(['--release'] if action=='release' else []),folder/'run.log')
            checks=worker('check',output,folder/'checks',['--spec',str(folder/'reference/inspect.json')])
            summary=read(folder/'result.json')
            event.update(summary={k:summary[k] for k in ('eligible','reserved','rule_areas','released','packages','seconds') if k in summary},
                         accepted=gate(previous,drc(output),checks,strict=False))
        except subprocess.CalledProcessError as error:
            event.update(status='worker_error',returncode=error.returncode)
        events.append(event)
        if action=='release' and not event['accepted']:raise RuntimeError('fanout release rejected; see '+str(folder))
        result=output if event['accepted'] else board
        phase(label,result,event,stop=False)
        return result
    emit('phase_start',board=current,data=dict(phase='usb-pairs' if a.early_pairs else (a.only_mode or 'native-refinement'),budget_seconds=a.seconds))
    if a.early_pairs:
        if not a.electrical_fab:raise ValueError('early pairs require source electrical policy')
        from pnr.paired_bootstrap import run as route_pairs
        from pnr.staged_signal import run as route_signals
        paired=route_pairs(current,a.rules,a.constraints,a.out_dir/'early-pairs',a.kicad_python,a.kicad_cli,allow_placement=(not a.route_only or a.early_pair_placement))
        phase('02-usb-pairs',paired,stop=False)
        policy=read(a.rules);policy['routed_pair_references']=read(paired.parent/'paired-reference.json');save(a.rules,policy)
        # A stop here must still publish the routed pair references to the policy.
        stop_after('02-usb-pairs',paired)
        # Complete high-priority electrical work before the ordinary maze can
        # consume its escape corridors. Ground access is a distinct native mode.
        # The second power sweep can reuse copper added by the earlier phases.
        powered=paired
        reserved=os.environ.get('PNR_FANOUT_RESERVE')=='1'
        if reserved:
            powered=fanout(paired,'reserve','02b-fanout-reserve');reserved=powered!=paired
        for phase_index,(name,mode,cycles,attempts,seconds) in enumerate((
                ('early-power','power',2,40,600),
                ('early-plane','plane',1,40,180),
                ('early-power-refine','power',2,60,600)),start=3):
            phase_args=[str(powered),'--repo',str(a.repo.resolve()),
                '--rules',str(a.rules.resolve()),'--constraints',str(a.constraints.resolve()),
                '--electrical-fab',str(a.electrical_fab.resolve()),'--only-mode',mode,
                '--cycles',str(cycles),'--route-attempts',str(attempts),
                '--placement-attempts','1','--search-seconds','10','--seconds',str(seconds),
                '--out-dir',str((a.out_dir/name).resolve()),
                '--kicad-python',a.kicad_python,'--kicad-cli',a.kicad_cli]
            if a.route_only:phase_args+=['--route-only']
            if os.environ.get('PNR_SHOVE')=='1':phase_args+=['--shove-origin-board',str(shove_origin)]
            for source in a.annotation_source:phase_args+=['--annotation-source',str(source.resolve())]
            # No early-pairs flag: phases cannot recursively bootstrap.
            powered=main(phase_args,nested=True)
            label=f'{phase_index:02d}-{name}'
            if reserved and label==stop_label:powered=fanout(powered,'release',f'{phase_index:02d}b-fanout-release');reserved=False
            phase(label,powered,read(a.out_dir/name/'progress.json'))
        if reserved:powered=fanout(powered,'release','05b-fanout-release')
        current=route_signals(powered,a.rules,a.constraints,a.out_dir/'staged-signal',a.kicad_python,a.kicad_cli)
        phase('06-signals',current)
    current=bank_consolidation(current,'06b-power-bank-consolidation')
    started=phase_clock.begin_refinement()
    before=drc(current);initial=before
    inv=worker('inspect',current,a.out_dir/'initial');original=inv['footprint_poses']
    def record(reason='running'):
        coverage=open_net_coverage(before,inv['item_nets'])
        save(a.out_dir/'progress.json',dict(source=str(a.board.resolve()),source_sha256=hashlib.sha256(a.board.read_bytes()).hexdigest(),best=str(current),best_sha256=hashlib.sha256(current.read_bytes()).hexdigest(),initial_opens=len(initial['unconnected_items']),opens=len(before['unconnected_items']),rounds=rounds,events=events,component_scores=history,termination=reason,elapsed_seconds=round(time.monotonic()-started,2),prerequisite_seconds=round(phase_clock.prerequisite_seconds,2),native_open_nets=dict(coverage),protected_open_nets={n:c for n,c in coverage.items() if n in inv['excluded']},electrical_modes_enabled=bool(a.electrical_fab),budgets=dict(cycles=a.cycles,route_attempts=a.route_attempts,placement_attempts=a.placement_attempts,seconds=a.seconds)))
    portal_trials=set()
    def route_sweep(source,inventory,folder,targets=None):
        nonlocal seq
        trial_current=source;report=drc(source); failures=[];accepted_count=0
        placement_trial=folder.name.startswith('move-')
        queue=scheduled_route_jobs(inventory['targets'] if targets is None else targets,
                                   {} if placement_trial else job_attempts)
        if a.only_mode:queue=[t for t in queue if t.get('mode','signal')==a.only_mode]
        # Speculate only additive ordinary single-net routes. Power, pairs,
        # rip-up and placement transactions retain their coupled serial paths.
        from pnr.runtime_controls import route_workers
        parallel_workers=route_workers('native-sweep')
        if parallel_workers>1 and not placement_trial:
            from pnr.speculative import evaluate_batch
            eligible=[];nets=set()
            for target in queue[:a.route_attempts]:
                if target.get('mode','signal')=='signal' and target['net'] not in nets:
                    eligible.append(target);nets.add(target['net'])
            batch_index=0
            while batch_index<len(eligible):
                parallel_workers=route_workers('native-batch')
                selected=eligible[batch_index:batch_index+parallel_workers];batch_index+=len(selected)
                if time.monotonic()-started>=a.seconds:break
                if len(selected)<2:continue
                snapshot=trial_current;paths={}
                budget=min(route_search_seconds(a.search_seconds,1),max(.001,a.seconds-(time.monotonic()-started)))
                for i,target in enumerate(selected):paths[route_job_key(target)]=folder/f'parallel-{batch_index+i:03d}'
                emit('parallel_route_start',data=dict(targets=selected,workers=parallel_workers,base=str(snapshot)))
                def propose(target,base):
                    rd=paths[route_job_key(target)];rd.mkdir(parents=True,exist_ok=True)
                    p,q=target['source_xy'],target['target_xy'];b=inventory['bounds'];margin=3
                    box=[max(b[0],min(p[0],q[0])-margin),max(b[1],min(p[1],q[1])-margin),min(b[2],max(p[0],q[0])+margin),min(b[3],max(p[1],q[1])+margin)]
                    cmd=[a.kicad_python,str(worker_root/'hardware/tools/keyhole_region.py'),str(base),'--out-dir',str(rd/'proposal'),'--source-pad',target['source'],'--target-pad',target['target'],'--net',target['net'],'--bounds',*map(str,box),'--layers','--joint','--preserve-copper','--pitch',str(a.pitch if hasattr(a,'pitch') else .2),'--max-expansions','150000','--max-orders','32','--max-seconds',str(budget),'--kicad-cli',a.kicad_cli,'--rules',str(a.rules.resolve())]
                    for source in a.annotation_source:cmd+=['--annotation-source',str(source.resolve())]
                    for end in ('source','target'):
                        if target.get(end+'_uuid'):cmd+=['--'+end+'-pad-uuid',target[end+'_uuid']]
                    invoke(cmd,rd/'proposal.log');return read(rd/'proposal/result.json')
                def commit(target,base,current,proposal):
                    nonlocal report,accepted_count
                    if not proposal.get('accepted') or time.monotonic()-started>=a.seconds:return None
                    rd=paths[route_job_key(target)];merged=rd/'merged.kicad_pcb'
                    invoke([a.kicad_python,'-m','pnr.merge_additive',str(base),str(rd/'proposal/candidate.kicad_pcb'),str(current),str(merged)],rd/'merge.log')
                    # merge_additive writes .kicad_pcb/.kicad_pro (+ the fab-profile
                    # .kicad_dru, which pnr.planes also writes before it refills: KiCad
                    # fills zones under the rules it reads at load). Without the
                    # footprint table every footprint is a lib_footprint_issues DRC
                    # violation and gate() rejects every parallel merge.
                    for origin in (current,base,rd/'proposal/candidate.kicad_pcb'):
                        if copy_lib_table(Path(origin),rd):break
                    invoke([a.kicad_python,'-m','pnr.planes',str(merged),'--rules',str(a.rules.resolve()),'--refill-only'],rd/'refill.log')
                    checks=worker('check',merged,rd/'checks',['--spec',str(folder/'reference.json')]);after=drc(merged)
                    if not gate(report,after,checks):return None
                    report=after;accepted_count+=1
                    emit('route_result',board=merged,data=dict(phase='parallel-single-track',target=target,accepted=True,opens=len(after['unconnected_items'])))
                    return merged
                trial_current,committed,retry,batch_events=evaluate_batch(selected,snapshot,propose,commit,parallel_workers)
                events.append(dict(stage='parallel_routes',proposals=batch_events,accepted=len(committed),serial_retry=len(retry)))
                queue=[t for t in queue if route_job_key(t) not in {route_job_key(c) for c in committed}]
                record()
        for target in queue[:a.route_attempts]:
            emit('route_start',data=dict(phase=a.only_mode or 'native-refinement',target=target,cycle=len(rounds)))
            if time.monotonic()-started>=a.seconds:break
            if not placement_trial:
                key=route_job_key(target);job_attempts[key]=job_attempts.get(key,0)+1
            attempt=max(1,job_attempts.get(route_job_key(target),1)) if placement_trial else job_attempts[key]
            terminal_repair=(attempt==2 and not placement_trial and target.get('mode','signal')=='signal')
            requested=route_search_seconds(a.search_seconds,attempt)
            if not terminal_repair:
                requested=max(requested,job_budget_hints.get(route_job_key(target),0))
            search_seconds=min(requested,max(.001, a.seconds - (time.monotonic()-started)))
            search_pitch=route_search_pitch(attempt)
            if terminal_repair:search_pitch=.1
            seq+=1;rd=folder/f'route-{seq:03d}'
            p,q=target['source_xy'],target['target_xy'];margin=3+2*(len(rounds)-1)
            if terminal_repair:margin=1.5
            bounds=inventory['bounds']
            box=[max(bounds[0],min(p[0],q[0])-margin),max(bounds[1],min(p[1],q[1])-margin),min(bounds[2],max(p[0],q[0])+margin),min(bounds[3],max(p[1],q[1])+margin)]
            cmd=[a.kicad_python,str(worker_root/'hardware/tools/keyhole_region.py'),str(trial_current),'--out-dir',str(rd),'--source-pad',target['source'],'--target-pad',target['target'],'--bounds',*map(str,box),'--net',target['net'],'--layers','--joint','--pitch',str(search_pitch),'--max-expansions','150000','--max-orders','32','--max-seconds',str(search_seconds),'--kicad-cli',a.kicad_cli,'--rules',str(a.rules.resolve())]
            reopened=[]
            if len(rounds)==1:
                cmd+=['--preserve-copper']
            else:
                cmd+=['--relocate-vias']
                counts=Counter()
                for identity,weight in failure_history.get(target['net'],{}).items():
                    net=inventory['item_nets'].get(identity)
                    if net and net!=target['net'] and net not in inventory['excluded']:
                        counts[net]+=weight
                reopened=[n for n,_ in counts.most_common(2)]
                if terminal_repair:
                    reopened=terminal_repair_nets(worker,trial_current,rd,target)
                for net in reopened:cmd+=['--net',net]
            for s in a.annotation_source:cmd+=['--annotation-source',str(s.resolve())]
            if a.electrical_fab and target.get('mode','signal')!='signal':
                cmd=[a.kicad_python,'-m','pnr.native_electrical',str(trial_current),'--rules',str(a.rules.resolve()),'--out-dir',str(rd),'--net',target['net'],'--source-pad',target['source'],'--target-pad',target['target'],'--bounds',*map(str,box),'--seconds',str(search_seconds),'--pitch',str(search_pitch),'--kicad-cli',a.kicad_cli]
                electrical_box=electrical_search_bounds(target.get('mode'),attempt,box,bounds,
                    enabled=os.environ.get('PNR_WIDE_POWER_SEARCH')=='1')
                if os.environ.get('PNR_SHOVE')=='1' and electrical_box==list(box):
                    # A3: trunks search the board; branches include their root copper.
                    from pnr.shove.targets import search_bounds
                    electrical_box=search_bounds(target,attempt,box,bounds)
                index=cmd.index('--bounds');cmd[index+1:index+5]=list(map(str,electrical_box))
                reopened=[]
            for end in ('source','target'):
                if target.get(end+'_uuid'):
                    cmd += ['--'+end+'-pad-uuid',target[end+'_uuid']]
            folder.mkdir(parents=True,exist_ok=True)
            try:
                invoke(cmd,folder/f'route-{seq:03d}.log');outcome=read(rd/'result.json')
            except subprocess.CalledProcessError:
                outcome=dict(status='worker_error',accepted=False)
            if a.electrical_fab and target.get('mode')=='pair' and not outcome.get('accepted'):
                policy=read(a.rules);pair=next(p for p in policy['diff_pairs'] if target['net'] in (p['p'],p['n']))
                proposals=pair_placements(inventory,a.constraints,pair)
                save(folder/f'pair-proposals-{seq:03d}.json',proposals)
                for pi,proposal in enumerate(diverse_pair_poses(proposals,4)):
                    if time.monotonic()-started>=a.seconds:break
                    spec=folder/f'pair-placement-{seq:03d}-{pi}.json';save(spec,proposal)
                    trial=folder/f'pair-placement-{seq:03d}-{pi}'
                    pcmd=list(cmd);pcmd[pcmd.index('--out-dir')+1]=str(trial);pcmd+=['--placement-spec',str(spec)]
                    try:
                        invoke(pcmd,trial.with_suffix('.log'));pout=read(trial/'result.json')
                    except subprocess.CalledProcessError:pout=dict(status='worker_error',accepted=False)
                    events.append(dict(stage='coupled_placement_trial',move=proposal,status=pout['status'],accepted=pout.get('accepted',False),folder=str(trial)))
                    if pout.get('accepted'):rd=trial;outcome=pout;break
            # A plane/power access may be trapped by an already routed signal.
            # Reopen only a bounded signal region and restore it atomically; the
            # adapter and this controller both compare with the original board.
            repair_key=route_job_key(target)
            if (a.electrical_fab and target.get('mode') in ('plane','power')
                    and not outcome.get('accepted') and len(rounds)>1
                    and repair_key not in electrical_repair_attempts
                    and len(electrical_repair_attempts)<8
                    and a.seconds-(time.monotonic()-started)>90):
                from pnr.electrical import net_policy
                counts=Counter();policy=read(a.rules)
                for identity,weight in outcome.get('via_blockers',{}).items():
                    net=inventory['item_nets'].get(identity)
                    if (net and inventory.get('item_kinds',{}).get(identity)=='PCB_TRACK'
                            and net_policy(net,policy)['mode']=='signal'):
                        # Through-via blockers on an inner layer are distinct
                        # from the surface route that surrounds the terminal.
                        inner=inventory.get('item_layers',{}).get(identity,'').startswith('In')
                        counts[net]+=weight*(2 if inner else 1)
                if counts:
                    electrical_repair_attempts.add(repair_key)
                    repair_box=[max(bounds[0],min(p[0],q[0])-2),max(bounds[1],min(p[1],q[1])-2),min(bounds[2],max(p[0],q[0])+2),min(bounds[3],max(p[1],q[1])+2)]
                    for bi,(net,_) in enumerate(counts.most_common(2)):
                        if a.seconds-(time.monotonic()-started)<60:break
                        trial=folder/f'electrical-repair-{seq:03d}-{bi}'
                        rcmd=[a.kicad_python,'-m','pnr.electrical_repair',str(trial_current),
                            '--rules',str(a.rules.resolve()),'--out-dir',str(trial),
                            '--net',target['net'],'--source-pad',target['source'],
                            '--target-pad',target['target'],'--blocker-net',net,
                            '--bounds',*map(str,repair_box),'--seconds',str(min(30,search_seconds)),
                            '--kicad-cli',a.kicad_cli,'--regional-adapter',str(worker_root/'hardware/tools/keyhole_region.py')]
                        for end in ('source','target'):
                            if target.get(end+'_uuid'):rcmd+=['--'+end+'-pad-uuid',target[end+'_uuid']]
                        try:
                            invoke(rcmd,trial.with_suffix('.log'));repair_outcome=read(trial/'result.json')
                        except subprocess.CalledProcessError:repair_outcome=dict(status='worker_error',accepted=False)
                        events.append(dict(stage='electrical_blocker_repair',target=target,blocker=net,
                            status=repair_outcome['status'],accepted=repair_outcome.get('accepted',False),folder=str(trial)))
                        if repair_outcome.get('accepted'):
                            rd=trial;outcome=repair_outcome;break
            # PNR_SHOVE=1: make room for a blocked power/plane route (pnr.shove):
            # copper shove, small part nudges, or a bounded signal rip-reroute. The
            # candidate still passes the unchanged outer gate below.
            if os.environ.get('PNR_SHOVE')=='1':
                from pnr.shove import control as shove_control
                if shove_control.eligible(target,outcome,True,a.electrical_fab,placement_trial,a.seconds-(time.monotonic()-started)):
                    import hashlib
                    board_hash=hashlib.sha256(trial_current.read_bytes()).hexdigest()
                    if shove_budget.reserve(board_hash,route_job_key(target)):
                        trial=folder/f'shove-{seq:03d}';spec=folder/f'shove-target-{seq:03d}.json'
                        save(spec,dict(target,static_blockers=outcome.get('static_blockers',{}),via_blockers=outcome.get('via_blockers',{})))
                        from pnr.shove.placement import protected_refs
                        parts=shove_control.nudge_candidates(inventory,a.constraints,[target['source_xy'],target['target_xy']],
                                                             exclude=protected_refs(read(a.rules)))
                        remaining=a.seconds-(time.monotonic()-started)
                        rcmd=[a.kicad_python,'-m','pnr.shove',str(trial_current),'--rules',str(a.rules.resolve()),
                              '--target-json',str(spec),'--out-dir',str(trial),'--kicad-cli',a.kicad_cli,
                              '--adapter',str(worker_root/'hardware/tools/keyhole_region.py'),
                              '--seconds',str(max(30.,min(150.,remaining-60))),'--parts',','.join(parts),
                              '--constraints',str(a.constraints.resolve()),'--placement-python',sys.executable,
                              '--origin-board',str(shove_origin),
                              '--bounds',*map(str,electrical_box if a.electrical_fab and target.get('mode','signal')!='signal' else box)]
                        for source in a.annotation_source:rcmd+=['--annotation-source',str(source.resolve())]
                        try:invoke(rcmd,trial.with_suffix('.log'));shove_outcome=read(trial/'result.json')
                        except (subprocess.CalledProcessError,FileNotFoundError):shove_outcome=dict(status='worker_error',accepted=False)
                        shove_budget.record(shove_outcome)
                        events.append(shove_control.event(target,shove_outcome,trial,shove_budget))
                        if shove_outcome.get('accepted'):rd=trial;outcome=shove_outcome
                        else:outcome=shove_control.merge_failure(outcome,shove_outcome,inventory.get('owners',{}))
            # Explicit opt-in experiment: restore an ordinary plane-return leaf
            # atomically with a trapped signal, not an unchecked clearance waiver.
            if (os.environ.get('PNR_PLANE_LEAF_REPAIR')=='1' and not placement_trial
                    and target.get('mode','signal')=='signal' and not outcome.get('accepted')
                    and a.seconds-(time.monotonic()-started)>100 and plane_leaf_budget.available):
                from pnr.plane_leaf_repair import endpoint_has_no_escape
                search_events=rd/'search-events.jsonl'
                rows=[json.loads(line) for line in search_events.read_text().splitlines()] if search_events.exists() else []
                for side in endpoint_has_no_escape(rows):
                    if a.seconds-(time.monotonic()-started)<70:break
                    end='source' if side==0 else 'target';ref=target[end].rsplit('.',1)[0]
                    import hashlib
                    leaf_board_hash=hashlib.sha256(trial_current.read_bytes()).hexdigest()
                    if not plane_leaf_budget.reserve(leaf_board_hash,ref,target['net']):continue
                    trial=folder/f'plane-leaf-{seq:03d}-{side}'
                    spec=folder/f'plane-leaf-target-{seq:03d}-{side}.json';save(spec,target)
                    rcmd=[a.kicad_python,'-m','pnr.plane_leaf_repair',str(trial_current),
                        '--rules',str(a.rules.resolve()),'--target-json',str(spec),'--out-dir',str(trial),
                        '--adapter',str(worker_root/'hardware/tools/keyhole_region.py'),'--kicad-cli',a.kicad_cli,
                        '--focus',ref,'--seconds',str(min(40,a.seconds-(time.monotonic()-started)-30))]
                    for source in a.annotation_source:rcmd+=['--annotation-source',str(source.resolve())]
                    try:invoke(rcmd,trial.with_suffix('.log'));repair_outcome=read(trial/'result.json')
                    except subprocess.CalledProcessError:repair_outcome=dict(status='worker_error',accepted=False)
                    plane_leaf_budget.record(leaf_board_hash,ref,repair_outcome)
                    events.append(dict(stage='plane_leaf_repair',target=target,focus=ref,
                        status=repair_outcome['status'],accepted=repair_outcome.get('accepted',False),folder=str(trial),budget=plane_leaf_budget.summary()))
                    if repair_outcome.get('accepted'):rd=trial;outcome=repair_outcome;break
            if (os.environ.get('PNR_POWER_DETOUR_REPAIR')=='1' and not placement_trial
                    and target.get('mode','signal')=='signal' and not outcome.get('accepted')
                    and a.seconds-(time.monotonic()-started)>160 and power_detour_budget.available):
                from pnr.plane_leaf_repair import endpoint_has_no_escape
                search_events=rd/'search-events.jsonl'
                rows=[json.loads(line) for line in search_events.read_text().splitlines()] if search_events.exists() else []
                for side in endpoint_has_no_escape(rows):
                    if a.seconds-(time.monotonic()-started)<100:break
                    focus=target['source' if side==0 else 'target']
                    import hashlib
                    board_hash=hashlib.sha256(trial_current.read_bytes()).hexdigest()
                    if not power_detour_budget.reserve(board_hash,focus,target['net']):continue
                    trial=folder/f'power-detour-{seq:03d}-{side}'
                    spec=folder/f'power-detour-target-{seq:03d}-{side}.json';save(spec,target)
                    rcmd=[a.kicad_python,'-m','pnr.power_detour_repair',str(trial_current),
                        '--rules',str(a.rules.resolve()),'--target-json',str(spec),'--out-dir',str(trial),
                        '--adapter',str(worker_root/'hardware/tools/keyhole_region.py'),'--kicad-cli',a.kicad_cli,
                        '--focus',focus,'--seconds',str(min(180,a.seconds-(time.monotonic()-started)-40))]
                    for source in a.annotation_source:rcmd+=['--annotation-source',str(source.resolve())]
                    try:invoke(rcmd,trial.with_suffix('.log'));repair_outcome=read(trial/'result.json')
                    except subprocess.CalledProcessError:repair_outcome=dict(status='worker_error',accepted=False)
                    power_detour_budget.record(repair_outcome)
                    events.append(dict(stage='power_detour_repair',target=target,focus=focus,
                        status=repair_outcome['status'],accepted=repair_outcome.get('accepted',False),folder=str(trial),budget=power_detour_budget.summary()))
                    if repair_outcome.get('accepted'):rd=trial;outcome=repair_outcome;break
            if (os.environ.get('PNR_PORTAL_REPAIR')=='1' and not placement_trial
                    and target.get('mode','signal')=='signal' and not outcome.get('accepted')
                    and len(portal_trials)<3):
                from pnr.portal_retry import retry_command
                portal_key=route_job_key(target)
                remaining=a.seconds-(time.monotonic()-started)
                trial=folder/f'portal-repair-{seq:03d}'
                retry=retry_command(cmd,trial,remaining)
                if portal_key not in portal_trials and retry:
                    portal_trials.add(portal_key)
                    try:invoke(retry,trial.with_suffix('.log'));portal_outcome=read(trial/'result.json')
                    except subprocess.CalledProcessError:portal_outcome=dict(status='worker_error',accepted=False)
                    events.append(dict(stage='portal_repair',target=target,status=portal_outcome['status'],
                        accepted=portal_outcome.get('accepted',False),folder=str(trial),command=retry))
                    if portal_outcome.get('accepted'):rd=trial;outcome=portal_outcome
            if not placement_trial and not terminal_repair and target.get('mode','signal')=='signal':
                hint=progress_budget_hint(outcome,search_seconds,a.search_seconds)
                if hint:job_budget_hints[route_job_key(target)]=hint
            event=dict(stage='route',target=target,folder=str(rd),status=outcome['status'],search_seconds=search_seconds,search_pitch=search_pitch,search_strategy='terminal_keyhole' if terminal_repair else 'broad',accepted=False,reopened_blocker_nets=reopened,scope='placement_trial' if folder.name.startswith('move-') else 'checkpoint')
            failure_history[target['net']]=outcome.get('static_blockers',{}) or failure_history.get(target['net'],{})
            if outcome.get('accepted'):
                board=rd/'candidate.kicad_pcb';after=read(board.with_suffix('.drc.json'))
                checks=worker('check',board,rd/'outer-check',['--spec',str(folder/'reference.json')]);after=drc(board)
                if gate(report,after,checks):trial_current=board;report=after;event['accepted']=True;accepted_count+=1
            if not event['accepted']:failures.append(dict(target=target,status=event['status'],static_blockers=outcome.get('static_blockers',{})))
            event['opens']=len(report['unconnected_items']);events.append(event);record()
            emit('route_result',board=trial_current,data=dict(event,phase=a.only_mode or 'native-refinement'))
        return trial_current,report,failures,accepted_count
    def congestion_snapshot(inventory, folder, label, summary):
        from pnr.graph import BoardGraph
        from pnr.congestion_diagnostics import snapshot, write_snapshot, native_endpoints
        graph=BoardGraph.from_json(json.dumps(inventory['graph']))
        write_snapshot(folder, native_endpoints(snapshot(graph, read(a.rules), label=label,
            unresolved={t['net'] for t in inventory['targets']},
            metadata=dict(summary=summary, component_scores=dict(history),
                          native_targets=inventory['targets'],
                          signal='Native unresolved terminal density; not measured free-track capacity')),inventory))

    reason='cycle_limit' 
    for cycle in range(1,a.cycles+1):
        if time.monotonic()-started>=a.seconds:reason='time_budget';break
        if not before['unconnected_items']:reason='zero_native_opens';break
        folder=a.out_dir/f'cycle-{cycle:02d}';folder.mkdir()
        inv=worker('inspect',current,folder/'start');save(folder/'reference.json',inv)
        congestion_snapshot(inv,folder/'congestion-before',f'Native P/R cycle {cycle}: before',f"{len(before['unconnected_items'])} native opens")
        entry=dict(cycle=cycle,before=len(before['unconnected_items']),placement_trials=0,placement_accepted=0,route_accepted=0)
        rounds.append(entry)
        current,before,failures,count=route_sweep(current,inv,folder)
        entry['route_accepted']+=count
        history=score_failures(history,failures,inv['owners'])
        inv=worker('inspect',current,folder/'after-route')
        candidates=[] if a.route_only else placements(inv,a.constraints,history,tried,original,a.max_move,read(a.rules))
        save(folder/'placement-candidates.json',candidates)
        if candidates:
            candidates=worker('placement-copper',current,folder/'copper-ranking',['--spec',str(folder/'placement-candidates.json')])
        for move in diverse_placement_trials(candidates,a.placement_attempts):
            if time.monotonic()-started>=a.seconds:break
            entry['placement_trials']+=1;md=folder/f'move-{entry["placement_trials"]:02d}';md.mkdir()
            tried.add((move['ref'],round(move['position'][0],6),round(move['position'][1],6)))
            save(md/'move.json',move);save(md/'reference.json',inv)
            board=md/'candidate.kicad_pcb';copy_board(current,board)
            worker('move',current,md,['--spec',str(md/'move.json'),'--out',str(board)])
            checks=worker('check',board,md,['--spec',str(md/'reference.json')]);after=drc(board)
            event=dict(stage='placement',move=move,accepted=False,folder=str(md),checks=checks,opens=len(after['unconnected_items']))
            # Translation and terminal tethers must first preserve the old board.
            if gate(before,after,checks,strict=False):
                mi=worker('inspect',board,md/'inventory');save(md/'reference.json',mi)
                candidate,result,more,n=route_sweep(board,mi,md,
                    targets=affected_route_jobs(mi,move['ref'],failure_history))
                final_checks=worker('check',candidate,md/'final-check',['--spec',str(folder/'after-route'/'inspect.json')]);result=drc(candidate)
                if gate(before,result,final_checks):
                    # Do not retain an incidental placement change when routing
                    # also works at the original pose with its original fanout.
                    spec=md/'unmove-spec.json';save(spec,dict(ref=move['ref'],before=str(current),moved=str(board)))
                    reverted=md/'reverted.kicad_pcb';copy_board(candidate,reverted)
                    undo=worker('unmove',candidate,md/'unmove',['--spec',str(spec),'--out',str(reverted)])
                    move_kept=True
                    if not undo.get('skipped'):
                        checks=worker('check',reverted,md/'unmove-check',['--spec',str(folder/'after-route'/'inspect.json')]);reverted_drc=drc(reverted)
                        if gate(before,reverted_drc,checks):candidate,result=reverted,reverted_drc;move_kept=False
                    current,before=candidate,result;event['accepted']=True;event['move_kept']=move_kept
                    entry['placement_accepted']+=int(move_kept);entry['route_accepted']+=n
                    # An incidental route that also works at the old pose must
                    # not cause this same rejected move to repeat next cycle.
                    if move_kept:tried.clear()
                history=score_failures(history,more,mi['owners'])
            else:event['status']='placement_native_guard'
            if 'status' not in event:
                event['status']=('placement_retained' if event.get('move_kept') else 'route_improved_original_pose') if event['accepted'] else 'no_route_improvement'
            event['violations']=dict(Counter(v['type'] for v in after['violations']))
            events.append(event);record()
            if event['accepted']:break
        end_inventory=worker('inspect',current,folder/'end-inventory')
        congestion_snapshot(end_inventory,folder/'congestion-after',f'Native P/R cycle {cycle}: after',f"{len(before['unconnected_items'])} native opens; {entry['placement_accepted']} retained moves")
        entry['after']=len(before['unconnected_items']);record()
        if time.monotonic()-started>=a.seconds:reason='time_budget';break
        if not before['unconnected_items']:reason='zero_native_opens';break
        # A failed sweep is not convergence. Exhaust the distinct legal move set
        # or the explicit cycle/time budget; retain native blocker history.
        if not candidates and not count and repair_strategies_exhausted(
                inv['targets'], job_attempts, a.only_mode, a.search_seconds):
            reason='no_legal_untried_moves';break
    if len(before['unconnected_items'])<len(initial['unconnected_items']):
        cleanup=a.out_dir/'cleanup';cleanup.mkdir()
        prior=worker('inspect',current,cleanup/'reference')
        out=cleanup/'candidate.kicad_pcb'
        cmd=[a.kicad_python,'-m','pnr.via_coalesce',str(current),'--out',str(out),'--rules',str(a.rules.resolve()),'--report',str(cleanup/'result.json'),'--work-dir',str(cleanup/'trials'),'--kicad-cli',a.kicad_cli]
        for source in a.annotation_source:cmd+=['--annotation-source',str(source.resolve())]
        invoke(cmd,cleanup/'cleanup.log')
        checks=worker('check',out,cleanup/'checks',['--spec',str(cleanup/'reference'/'inspect.json')]);after=drc(out)
        accepted=gate(before,after,checks,strict=False)
        events.append(dict(stage='coalesce_and_graph',accepted=accepted,folder=str(cleanup)))
        if accepted:current,before=out,after
    current=bank_consolidation(current,'08b-power-bank-consolidation')
    result=a.out_dir/'best'/'candidate.kicad_pcb';copy_board(current,result);current=result;before=drc(current,final=True);record(reason)
    print(json.dumps(dict(best=str(current),opens=len(before['unconnected_items']),cycles=len(rounds),termination=reason)))
    phase('07-native-refinement',result,read(a.out_dir/'progress.json'))
    return result


if __name__=='__main__':
 from pnr.profile import run
 run('native-loop-worker' if '--worker' in sys.argv else 'native-loop',main)
