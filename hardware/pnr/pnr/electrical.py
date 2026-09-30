"""Source-derived electrical routing policy. No KiCad or YAML dependency.

IPC-2221 is an explicit screening model, not an IPC-2152 thermal sign-off.
Widths are lower bounds; explicit class widths never override a larger computed
requirement. Inner and outer copper are independently sized. Unknown current
is reported, never inferred from the width of an old trace.
"""
import hashlib
import os
import json
import math
from pathlib import Path
from pnr.plane_intent import positive, size_array


def current_width(current_a, copper_oz, delta_t_c, external=True):
    current_a=positive(current_a,'current_a')
    copper_oz=positive(copper_oz,'copper_oz')
    delta_t_c=positive(delta_t_c,'delta_t_c')
    k=.048 if external else .024
    return (current_a/(k*delta_t_c**.44))**(1/.725)/(1.378*copper_oz)*.0254


TERMINAL_WIDTH_TAG='# @pnr-terminal-width '
TERMINAL_WIDTH_KEYS={'target','pads','scope','net','min_width_mm','preferred_width_mm'}


def terminal_min_width_enabled():
    """PNR_TERMINAL_MIN_WIDTH=1 emits and applies @pnr-terminal-width contracts.

    Parsing and resolution always run (a malformed or stale annotation fails the
    compile either way); with the flag off nothing reaches rules.json or geometry.
    """
    return os.environ.get('PNR_TERMINAL_MIN_WIDTH')=='1'


def terminal_width_annotation(text):
    """Validate one ``@pnr-terminal-width`` JSON body (``@pnr-current`` conventions).

    ``target`` is an instance-address suffix and ``pads`` atomic pin numbers, as for
    @pnr-current; ``scope`` must be ``terminal``. ``min_width_mm`` is the hard entry
    floor, ``preferred_width_mm`` (default: the minimum) is tried first. The optional
    ``net`` asserts the resolved net. Unknown keys are rejected (typo guard).
    """
    a=json.loads(text)
    if not isinstance(a,dict):raise ValueError('terminal width annotation must be an object')
    unknown=set(a)-TERMINAL_WIDTH_KEYS
    if unknown:raise ValueError('unknown terminal width keys: '+', '.join(sorted(unknown)))
    if a.get('scope')!='terminal':raise ValueError('terminal width scope must be "terminal"')
    if not isinstance(a.get('target'),str) or not a['target'].strip('.'):raise ValueError('terminal width target required')
    pads=a.get('pads')
    if not isinstance(pads,list) or not pads or not all(isinstance(x,str) and x for x in pads) or len(set(pads))!=len(pads):
        raise ValueError('terminal width pads must be distinct pin names')
    if 'net' in a and (not isinstance(a['net'],str) or not a['net']):raise ValueError('terminal width net must be a name')
    a['min_width_mm']=positive(a.get('min_width_mm'),'min_width_mm')
    a['preferred_width_mm']=positive(a.get('preferred_width_mm',a['min_width_mm']),'preferred_width_mm')
    if a['preferred_width_mm']<a['min_width_mm']:raise ValueError('preferred width below minimum width')
    a['contract']='terminal_width'
    return a


def annotations(paths):
    """Source @pnr-current records, plus @pnr-terminal-width records marked
    ``contract='terminal_width'`` (resolve_currents/compile_policy route them)."""
    out=[]
    for path in paths:
        path=Path(path);raw=path.read_bytes()
        for line,text in enumerate(raw.decode().splitlines(),1):
            if text.strip().startswith(TERMINAL_WIDTH_TAG):
                a=terminal_width_annotation(text.strip().split(TERMINAL_WIDTH_TAG,1)[1])
                a['source']=dict(path=str(path),line=line,sha256=hashlib.sha256(raw).hexdigest())
                out.append(a)
                continue
            if text.strip().startswith('# @pnr-current '):
                a=json.loads(text.strip().split('# @pnr-current ',1)[1])
                if a.get('scope','net') not in ('net','terminal'):raise ValueError('invalid current scope')
                if not a.get('target') or not a.get('pads'):raise ValueError('current target/pads required')
                positive(a.get('rms_current_a'),'rms_current_a')
                positive(a.get('peak_current_a'),'peak_current_a')
                if a['peak_current_a']<a['rms_current_a']:raise ValueError('peak below RMS')
                a['source']=dict(path=str(path),line=line,sha256=hashlib.sha256(raw).hexdigest())
                out.append(a)
    return out


def resolve_currents(records, components):
    """Resolve stable instance addresses + pin numbers, never ref-based guesses."""
    out=[];claimed=set()
    for a in records:
        target=a['target'].strip('.')
        width=a.get('contract')=='terminal_width'
        kind='terminal width' if width else 'current'
        matches=[c for c in components if c.address.removesuffix('._p')==target or c.address.removesuffix('._p').endswith('.'+target)]
        if not matches and os.environ.get('PNR_SUBBOARD')=='1':continue  # target lies in another block
        if len(matches)!=1:raise ValueError('ambiguous/missing '+kind+' target '+target)
        c=matches[0];pads=[p for p in c.pads if p.name in a['pads']]
        if set(p.name for p in pads)!=set(a['pads']) or len(set(p.net for p in pads))!=1 or not pads[0].net:
            raise ValueError(kind+' pins missing or span multiple nets '+target)
        if width:
            # Exact source pins on the asserted net; one contract per terminal pad.
            if a.get('net') is not None and pads[0].net!=a['net']:
                raise ValueError('terminal width net mismatch '+target+': '+pads[0].net+' != '+a['net'])
            keys={(c.ref,n) for n in a['pads']}
            if keys & claimed:raise ValueError('overlapping terminal width contracts '+target)
            claimed|=keys
        out.append(dict(a,ref=c.ref,net=pads[0].net))
    return out


def compile_policy(rules, currents, fab):
    """Carry provenance and electrical budgets through the native JSON seam.

    A sub-board (PNR_SUBBOARD=1) resolves only its own parts' intents, but a
    rail's envelope is set by declarations anywhere on the parent board. The
    incoming rules then hold the parent's electrical_nets already restricted to
    nets present on the sub-board (pnr.hier.blocks.sub_board); their envelopes
    seed the policy so it equals the full-board policy on every present net.
    Widths and via arrays are still recomputed here from this fab/net_classes.

    Resolved @pnr-terminal-width records never enter current_intents. They are
    emitted as ``terminal_width_intents`` only with PNR_TERMINAL_MIN_WIDTH=1, so
    with the flag off rules.json is unchanged.
    """
    widths=[a for a in currents if a.get('contract')=='terminal_width']
    currents=[a for a in currents if a.get('contract')!='terminal_width']
    result=json.loads(json.dumps(rules));result['electrical_fab']=dict(fab)
    result['current_intents']=currents
    if terminal_min_width_enabled():result['terminal_width_intents']=widths
    policies={};inherited=set()
    if os.environ.get('PNR_SUBBOARD')=='1':
        for net,q in result.get('electrical_nets',{}).items():
            if 'rms_current_a' not in q or 'peak_current_a' not in q:continue
            policies[net]=dict(rms_current_a=q['rms_current_a'],peak_current_a=q['peak_current_a'],sources=list(q.get('sources',[])))
            inherited.add(net)
    key=lambda s:(s.get('line'),s.get('sha256'))
    for a in currents:
        if a.get('scope','net')!='net':continue
        p=policies.setdefault(a['net'],dict(rms_current_a=0,peak_current_a=0,sources=[]))
        # Multiple declarations describe envelopes of the same net, not loads
        # to sum. Branch load aggregation must happen in source design intent.
        p['rms_current_a']=max(p['rms_current_a'],a['rms_current_a'])
        p['peak_current_a']=max(p['peak_current_a'],a['peak_current_a'])
        # An inherited envelope already cites this annotation line (the parent
        # read it from another snapshot path); cite each source line once.
        if a['net'] not in inherited or all(key(s)!=key(a['source']) for s in p['sources']):p['sources'].append(a['source'])
    result['electrical_nets']=policies
    return size_policies(result)


def size_policies(result):
    """(Re)derive every net envelope's widths and via array from rules['electrical_fab'].

    Shared by compile_policy and pnr.fab_profile.apply_rules (a new copper model
    must resize existing envelopes). Envelopes (currents, sources) are untouched.
    """
    fab=result['electrical_fab']
    for net,p in result.get('electrical_nets',{}).items():
        if 'rms_current_a' not in p:continue
        base=max([result.get('fab',{}).get('track_width_mm',.2)]+[c['width_mm'] for c in result.get('net_classes',[]) if net in c['nets'] and c.get('width_mm')])
        p['outer_width_mm']=max(base,current_width(p['rms_current_a'],fab['outer_copper_oz'],fab['delta_t_c'],True))
        p['inner_width_mm']=max(base,current_width(p['rms_current_a'],fab['inner_copper_oz'],fab['delta_t_c'],False))
        p['via_array']=size_array(p,fab)
        p['model']='IPC-2221 screening; explicit current envelope; thermal qualification separate'
    return result


def net_policy(net,rules):
    p=dict(rules.get('electrical_nets',{}).get(net,{}))
    classes=[c for c in rules.get('net_classes',[]) if net in c.get('nets',[])]
    width=max([rules.get('fab',{}).get('track_width_mm',.2)]+[c['width_mm'] for c in classes if c.get('width_mm')])
    p.setdefault('outer_width_mm',width);p.setdefault('inner_width_mm',width)
    p['plane']=next((c['plane_layer'] for c in classes if c.get('plane_layer')),None)
    p['clearance_mm']=max([rules.get('fab',{}).get('clearance_mm',.15)]+[c['clearance_mm'] for c in classes if c.get('clearance_mm')])
    p['current_known']='rms_current_a' in p
    p['mode']='plane' if p['plane'] else ('power' if width>rules.get('fab',{}).get('track_width_mm',.2) or p['current_known'] else 'signal')
    for pair in rules.get('diff_pairs',[]):
        if net in (pair['p'],pair['n']):p.update(mode='pair',pair=pair)
    return p


def resolve_pair_chains(rules, paths, components):
    """Pair terminal topology is source-authored by instance address and pin."""
    result=json.loads(json.dumps(rules))
    for path in paths:
        raw=Path(path).read_bytes()
        for line,text in enumerate(raw.decode().splitlines(),1):
            if not text.strip().startswith('# @pnr-pair '):continue
            a=json.loads(text.strip().split('# @pnr-pair ',1)[1])
            pair=next((p for p in result['diff_pairs'] if p['name']==a['name']),None)
            if pair is None and os.environ.get('PNR_SUBBOARD')=='1':continue  # pair lies outside this block
            if pair is None:raise ValueError('annotated pair not declared: '+a['name'])
            def endpoint(spec):
                out={}
                for polarity in ('p','n'):
                    target,num=spec[polarity].rsplit(':',1)
                    cs=[c for c in components if c.address.removesuffix('._p')==target or c.address.removesuffix('._p').endswith('.'+target)]
                    if len(cs)!=1:raise ValueError('ambiguous pair target '+target)
                    ps=[p for p in cs[0].pads if p.name==num]
                    if len(ps)!=1 or ps[0].net!=pair[polarity]:raise ValueError('pair pin/net mismatch')
                    out[polarity]=cs[0].ref+'.'+num
                return out
            pair['terminal_chain']=[endpoint(t) for t in a['terminal_chain']]
            pair['auxiliary_pairs']=[dict(source=endpoint(t['source']),target=endpoint(t['target']),max_length_mm=positive(t['max_length_mm'],'auxiliary max length')) for t in a.get('auxiliary_pairs',[])]
            pair['max_uncoupled_mm']=positive(a['max_uncoupled_mm'],'max_uncoupled_mm')
            pair['reference_layer']=a['reference_layer']
            pair['source']=dict(path=str(path),line=line,sha256=hashlib.sha256(raw).hexdigest())
    return result


def terminal_policy(ref, pad_numbers, net, rules):
    """A leaf's load budget may differ from the shared rail trunk budget.

    Only fully annotated isolated terminal groups qualify. Distribute no current
    implicitly among parallel pads; summing declared terminal budgets is safe.
    """
    numbers=set(pad_numbers);covered=set();records=[]
    for a in rules.get('current_intents',[]):
        if a.get('scope')=='terminal' and a['ref']==ref and a['net']==net and set(a['pads']) & numbers:
            matched = set(a['pads']) & numbers
            if covered & matched:raise ValueError('overlapping terminal current contracts')
            # A subset receives the entire declared group budget. Never infer
            # equal current sharing merely because an annotation names N pads.
            covered.update(matched);records.append(a)
    if covered!=numbers:return None
    p=net_policy(net,rules);fab=rules['electrical_fab']
    p.update(rms_current_a=sum(a['rms_current_a'] for a in records),peak_current_a=sum(a['peak_current_a'] for a in records),current_known=True,terminal_sources=records)
    floor=rules.get('fab',{}).get('track_width_mm',.2)
    p['outer_width_mm']=max(floor,current_width(p['rms_current_a'],fab['outer_copper_oz'],fab['delta_t_c']))
    p['inner_width_mm']=max(floor,current_width(p['rms_current_a'],fab['inner_copper_oz'],fab['delta_t_c'],False))
    p['via_array']=size_array(p,fab)
    # PNR_TERMINAL_MIN_WIDTH=1: a source width contract on the same pads is a hard
    # floor on top of the current-derived width (never a reduction).
    contract=terminal_width(ref,numbers,net,rules)
    if contract:
        p['outer_width_mm']=max(p['outer_width_mm'],contract['min_width_mm'])
        p['inner_width_mm']=max(p['inner_width_mm'],contract['min_width_mm'])
        p.update(min_width_mm=contract['min_width_mm'],preferred_width_mm=contract['preferred_width_mm'],width_sources=contract['sources'])
    return p


def terminal_width(ref, pad_numbers, net, rules):
    """Source min/preferred width contract (PNR_TERMINAL_MIN_WIDTH=1) or None.

    Independent of current: a decoupling capacitor's ground terminal carries no
    DC budget yet has a datasheet trace-width rule. Keyed by (ref, pad, net) of
    the resolved ``terminal_width_intents``. For a pad group, any member's floor
    applies to the group (the widest minimum and preferred widths win).
    """
    if not terminal_min_width_enabled():return None
    numbers={pad_numbers} if isinstance(pad_numbers,str) else set(pad_numbers)
    records=[a for a in rules.get('terminal_width_intents',[]) if a['ref']==ref and a['net']==net and set(a['pads'])&numbers]
    if not records:return None
    minimum=max(a['min_width_mm'] for a in records)
    preferred=max([minimum]+[a.get('preferred_width_mm',a['min_width_mm']) for a in records])
    return dict(min_width_mm=minimum,preferred_width_mm=preferred,sources=[a['source'] for a in records if 'source' in a])


def neck_budget(policy, width, length_mm, fab):
    records=policy.get('terminal_sources',[])
    if len(records)!=1 or length_mm>records[0].get('neck_max_length_mm',0)+1e-6 or length_mm<=0:return None
    thickness=positive(fab['outer_copper_oz'],'outer copper')*1.378*.0254
    resistance=positive(fab['copper_resistivity_ohm_mm'],'resistivity')*length_mm/(positive(width,'neck width')*thickness)
    loss=policy['rms_current_a']**2*resistance;drop=policy['peak_current_a']*resistance
    if loss>positive(fab['neck_loss_budget_w'],'neck loss') or drop>positive(fab['neck_peak_drop_v'],'neck drop'):return None
    return dict(length_mm=length_mm,width_mm=width,resistance_ohm=resistance,loss_w=loss,peak_drop_v=drop,basis='source-bounded short neck; resistive loss/drop screen, thermal qualification separate')
