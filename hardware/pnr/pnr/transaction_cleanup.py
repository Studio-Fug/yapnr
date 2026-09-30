"""Remove only newly dangling ordinary signal vias after a routed transaction.

Native reports nominate; real layer contacts, source protection and final global
checks decide. A worker failure never replaces the input checkpoint.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys
from pathlib import Path

def read(p): return json.loads(Path(p).read_text())
def save(p, value): Path(p).write_text(json.dumps(value, indent=2)+'\n')
def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def newly_dangling(before, after):
    def ids(report):
        return {i['uuid'] for v in report['violations'] if v['type']=='via_dangling' for i in v['items']}
    return sorted(ids(after)-ids(before))

def eligible(item, requested, nets, excluded, mode, contact_layers):
    return (item['uuid'] in requested and item['kind']=='PCB_VIA' and not item['locked']
            and item['net'] in nets and item['net'] not in excluded and mode=='signal'
            and len(set(contact_layers))<=1)

def native_worker(a, board):
    import pcbnew as k
    from pnr.via_coalesce import uid, partition, protected, touch, copper_layers
    from pnr.pad_entry import snapshot
    from pnr.electrical import net_policy
    from pnr.native_electrical import reference_failures
    from pnr.electrical_audit import audit_board
    rules=read(a.rules); board.BuildConnectivity()
    if a.worker=='audit':
        save(a.report,dict(partition=partition(board),entries=snapshot(board,rules),
            reference_failures=reference_failures(board,rules),
            electrical=audit_board(board,rules,a.board.read_text())))
        return
    if a.worker=='fill':
        k.ZONE_FILLER(board).Fill(board.Zones());board.BuildConnectivity();k.SaveBoard(str(a.out),board)
        save(a.report,dict(filled=True));return
    spec=read(a.spec)
    if digest(a.board)!=spec['sha256']: raise ValueError('stale cleanup input')
    excluded,_=protected(board,rules,a.annotation_source)
    tracks=list(board.GetTracks());pads=[p for f in board.GetFootprints() for p in f.Pads()]
    removed=[];decisions=[]
    for via in tracks:
        identity=uid(via)
        if identity not in spec['requested'] or via.GetClass()!='PCB_VIA': continue
        net=via.GetNetname();layers=[]
        for la in copper_layers(board):
            if any(uid(x)!=identity and x.GetNetCode()==via.GetNetCode() and touch(x,via,la) for x in tracks+pads): layers.append(la)
            elif any(not z.GetIsRuleArea() and z.GetNetCode()==via.GetNetCode() and z.IsOnLayer(la)
                     and z.GetFilledPolysList(la).Collide(via.GetEffectiveShape(la),0) for z in board.Zones()): layers.append(la)
        item=dict(uuid=identity,kind=via.GetClass(),locked=via.IsLocked(),net=net)
        allowed=eligible(item,spec['requested'],spec['nets'],excluded,net_policy(net,rules)['mode'],layers)
        decisions.append(dict(**item,layers=[board.GetLayerName(la) for la in layers],removed=allowed))
        if allowed: removed.append(via)
    removed_ids=[uid(t) for t in removed]
    # Discarded: Delete, not Remove (a Removed via outlives its board; see
    # pnr.fanout_reserve.release). Normal interpreter exit is mandatory.
    for via in removed: board.Delete(via)
    board.BuildConnectivity();k.SaveBoard(str(a.out),board)
    save(a.report,dict(removed=removed_ids,decisions=decisions))

def guards(original, routed, final, before, candidate, after):
    from pnr.via_coalesce import preserved, acceptable
    checks=dict(preserved=preserved(before['partition'],after['partition']) and preserved(candidate['partition'],after['partition']),
        lost_pad_entries=[u for audit in (before,candidate) for u,v in audit['entries'].items() if v and not after['entries'].get(u)],
        new_bad_entries=[u for u,v in after['entries'].items() if not v and u not in before['entries']],reference_failures=after['reference_failures'])
    ok=(acceptable(original,final,checks) and len(final['unconnected_items'])<len(original['unconnected_items'])
        and len(final['unconnected_items'])<=len(routed['unconnected_items']) and not checks['new_bad_entries'] and not checks['reference_failures']
        and after['electrical']['subwidth_track_count']<=before['electrical']['subwidth_track_count'])
    return bool(ok),checks

def run(a):
    from pnr.native_drc import run_drc
    from pnr.live import emit
    a.out_dir.mkdir(parents=True,exist_ok=False);out=a.out_dir;exits=[]
    original=a.baseline;source=a.board;initial=digest(source);baseline_hash=digest(original)
    def worker(mode,board,label,*extra):
        report=out/(label+'.json');cmd=[a.kicad_python,'-m','pnr.transaction_cleanup',str(board),'--rules',str(a.rules),'--worker',mode,'--report',str(report),*map(str,extra)]
        for s in a.annotation_source:cmd+=['--annotation-source',str(s)]
        with (out/(label+'.log')).open('w') as log:p=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT)
        exits.append(dict(mode=mode,returncode=p.returncode));save(out/'worker-exits.json',exits);p.check_returncode();return read(report)
    def drc(board,name):return run_drc(a.kicad_cli,board,out/(name+'.drc.json'))
    result=dict(accepted=False,source=str(source),source_sha256=initial,baseline_sha256=baseline_hash)
    try:
        before=drc(original,'baseline');routed=drc(source,'routed');current=source;events=[]
        old=worker('audit',original,'baseline-audit');candidate=worker('audit',source,'routed-audit')
        for iteration in range(3):
            now=routed if iteration==0 else drc(current,'intermediate-'+str(iteration))
            dead=newly_dangling(before,now)
            if not dead: break
            new=out/f'pruned-{iteration}.kicad_pcb'
            shutil.copyfile(source.with_suffix('.kicad_pro'),new.with_suffix('.kicad_pro'))
            table=source.parent/'fp-lib-table'
            if table.exists():(out/'fp-lib-table').write_text(table.read_text().replace('${KIPRJMOD}',str(source.parent.resolve())))
            spec=out/f'prune-{iteration}-spec.json';save(spec,dict(sha256=digest(current),requested=dead,nets=a.net))
            event=worker('prune',current,f'prune-{iteration}','--spec',spec,'--out',new);events.append(event)
            if not event['removed']:break
            worker('fill',new,f'fill-{iteration}','--out',new);current=new
        if current==source:result.update(status='no_eligible_new_dangling',events=events)
        else:
            after=drc(current,'final');new=worker('audit',current,'final-audit');ok,checks=guards(before,routed,after,old,candidate,new)
            result.update(status='accepted' if ok else 'native_guard',accepted=ok,checks=checks,events=events,before_opens=len(before['unconnected_items']),after_opens=len(after['unconnected_items']),violations=after['violations'])
            if ok:
                for suffix in ('.kicad_pcb','.kicad_pro'):shutil.copyfile(current.with_suffix(suffix),out/('candidate'+suffix))
                shutil.copyfile(out/'final.drc.json',out/'candidate.drc.json')
    except subprocess.CalledProcessError as e:result.update(status='worker_error',returncode=e.returncode)
    result['inputs_unchanged']=initial==digest(source) and baseline_hash==digest(original)
    save(out/'result.json',result)
    emit('route_result',board=out/'candidate.kicad_pcb' if result['accepted'] else original,data=dict(phase='transaction-via-cleanup',**result))
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','checks')}))

def main():
    p=argparse.ArgumentParser();p.add_argument('board',type=Path);p.add_argument('--rules',type=Path,required=True);p.add_argument('--baseline',type=Path);p.add_argument('--out-dir',type=Path);p.add_argument('--net',action='append',default=[]);p.add_argument('--annotation-source',action='append',type=Path,default=[]);p.add_argument('--kicad-python',default=os.environ.get('PNR_KICAD_PYTHON','/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3'));p.add_argument('--kicad-cli',default=os.environ.get('PNR_KICAD_CLI','/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'));p.add_argument('--worker',choices=['audit','prune','fill']);p.add_argument('--report',type=Path);p.add_argument('--spec',type=Path);p.add_argument('--out',type=Path);a=p.parse_args()
    if a.worker:
        import pcbnew as k
        from pnr.fab_profile import load_board  # custom rules in force for the 'fill' worker
        board=load_board(a.board);native_worker(a,board)
    else:
        if not a.baseline or not a.out_dir:p.error('baseline and new out-dir required')
        run(a)
if __name__=='__main__':main()
