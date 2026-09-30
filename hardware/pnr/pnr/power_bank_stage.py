"""Source-current-sized whole-bank consolidation through isolated native gates.

Quality-only transactions; never imply electrical qualification or closed opens.
Workers must exit normally. A crash or failed guard retains the preceding board.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def read(path):return json.loads(Path(path).read_text())
def save(path,value):Path(path).write_text(json.dumps(value,indent=2)+'\n')
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def copy_board(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ('.kicad_pcb','.kicad_pro'):
        shutil.copyfile(source.with_suffix(suffix),target.with_suffix(suffix))
    table=source.parent/'fp-lib-table'
    if table.exists():
        (target.parent/'fp-lib-table').write_text(table.read_text().replace('${KIPRJMOD}',str(source.parent.resolve())))


def quality_checks(before,after,old,new):
    from pnr.via_coalesce import preserved,acceptable
    checks=dict(preserved=preserved(old['partition'],new['partition']),
        lost_pad_entries=[u for u,v in old['entries'].items() if v and not new['entries'].get(u)],
        new_bad_entries=[u for u,v in new['entries'].items() if not v and u not in old['entries']],
        reference_failures=new['reference_failures'])
    accepted=bool(acceptable(before,after,checks) and not checks['new_bad_entries']
        and not checks['reference_failures'] and new['vias']<old['vias']
        and new['electrical']['subwidth_track_count']<=old['electrical']['subwidth_track_count'])
    return accepted,checks


def execute_native(a,rules,board):
    import pcbnew as k
    from pnr.power_bank_reuse import proposals,apply
    from pnr.native_electrical import uid,reference_failures
    from pnr.via_coalesce import partition
    from pnr.pad_entry import snapshot
    from pnr.electrical_audit import audit_board
    board.BuildConnectivity()
    if a.worker=='inventory':
        result=dict(proposals=proposals(board,rules,a.radius),sha256=digest(a.board))
    elif a.worker=='apply':
        transaction=read(a.transaction)
        if transaction['sha256']!=digest(a.board):raise ValueError('stale source board')
        # apply deletes the reused bank's vias and returns their uuids.
        removed=apply(board,transaction['proposal'],rules)
        board.BuildConnectivity();k.SaveBoard(str(a.out),board)
        result=dict(removed_vias=len(removed),proposal=transaction['proposal'])
    elif a.worker=='prune':
        transaction=read(a.transaction);net=transaction['proposal']['net'];dead=set(a.prune)
        cut=[t for t in board.GetTracks() if uid(t) in dead and t.GetClass()=='PCB_TRACK'
             and not t.IsLocked() and t.GetNetname()==net and t.GetLength()/1e6<2.]
        result=dict(removed=[uid(t) for t in cut])
        for item in cut:board.Delete(item)  # discarded dead tails (Delete, not Remove)
        board.BuildConnectivity();k.SaveBoard(str(a.out),board)
    elif a.worker=='fill':
        k.ZONE_FILLER(board).Fill(board.Zones());board.BuildConnectivity();k.SaveBoard(str(a.out),board)
        result=dict(filled=True)
    elif a.worker=='audit':
        result=dict(partition=partition(board),entries=snapshot(board,rules),
            reference_failures=reference_failures(board,rules),
            electrical=audit_board(board,rules,a.board.read_text()),
            vias=sum(t.GetClass()=='PCB_VIA' for t in board.GetTracks()))
    save(a.report,result)


def consolidate(a):
    from pnr.native_drc import run_drc
    from pnr.live import emit
    if a.out is None or a.work_dir is None:raise ValueError('--out and --work-dir required')
    if a.max_trials<0 or a.radius<=0:raise ValueError('invalid trial budget or radius')
    if a.out.exists() or a.work_dir.exists():raise ValueError('new output and work directory required')
    a.work_dir.mkdir(parents=True)
    current=a.work_dir/'baseline.kicad_pcb';copy_board(a.board,current)
    initial_hash=digest(a.board);initial_rules_hash=digest(a.rules);events=[];worker_exits=[]
    def worker(mode,board,report,extra=()):
        cmd=[a.kicad_python,'-m','pnr.power_bank_stage',str(board),'--worker',mode,
             '--rules',str(a.rules),'--report',str(report),'--radius',str(a.radius)]+list(map(str,extra))
        started=time.monotonic()
        from pnr.proc import run as run_bounded
        with report.with_suffix('.log').open('w') as log:
            code=run_bounded(cmd,stdout=log,stderr=subprocess.STDOUT)
        worker_exits.append(dict(mode=mode,returncode=code,seconds=time.monotonic()-started))
        save(a.work_dir/'worker-exits.json',worker_exits)
        if code:raise subprocess.CalledProcessError(code,cmd)
        return read(report)
    def drc(board):return run_drc(a.kicad_cli,board,board.with_suffix('.drc.json'))
    before=drc(current);initial=before
    old=worker('audit',current,a.work_dir/'baseline.audit.json');initial_audit=old
    inventory=worker('inventory',current,a.work_dir/'inventory.json')
    queue=list(inventory['proposals']);reason='candidate_pool_exhausted'
    while queue and len(events)<a.max_trials:
        proposal=queue.pop(0);trial=a.work_dir/f'trial-{len(events):03d}';trial.mkdir()
        candidate=trial/'candidate.kicad_pcb';copy_board(current,candidate)
        transaction=trial/'transaction.json';save(transaction,dict(sha256=digest(current),proposal=proposal))
        event=dict(proposal=proposal,accepted=False,folder=str(trial))
        try:
            worker('apply',current,trial/'edit.json',['--transaction',transaction,'--out',candidate])
            worker('fill',candidate,trial/'fill.json',['--out',candidate]);after=drc(candidate)
            old_dead={i['uuid'] for v in before['violations'] if v['type']=='track_dangling' for i in v['items']}
            for n in range(4):
                dead={i['uuid'] for v in after['violations'] if v['type']=='track_dangling' for i in v['items']}-old_dead
                if not dead:break
                extra=['--transaction',transaction,'--out',candidate]
                for u in sorted(dead):extra+=['--prune',u]
                pruned=worker('prune',candidate,trial/f'prune-{n}.json',extra)
                if not pruned['removed']:break
                worker('fill',candidate,trial/f'refill-{n}.json',['--out',candidate]);after=drc(candidate)
            new=worker('audit',candidate,trial/'audit.json')
            accepted,checks=quality_checks(before,after,old,new)
            event.update(accepted=accepted,checks=checks,opens=len(after['unconnected_items']),
                violations=dict(Counter(v['type'] for v in after['violations'])),
                before_vias=old['vias'],after_vias=new['vias'],
                before_subwidth=old['electrical']['subwidth_track_count'],after_subwidth=new['electrical']['subwidth_track_count'])
            if accepted:
                current,before,old=candidate,after,new
                inventory=worker('inventory',current,trial/'next-inventory.json')
                queue=list(inventory['proposals'])
        except subprocess.CalledProcessError as error:
            event.update(status='worker_error',worker_returncode=error.returncode)
            events.append(event);reason='worker_error';break
        events.append(event)
        emit('route_result',board=current,data=dict(phase='power-bank-consolidation',**event))
    if queue and len(events)>=a.max_trials:reason='trial_limit'
    copy_board(current,a.out);final=drc(a.out)
    if final['unconnected_items']!=before['unconnected_items'] or Counter(v['type'] for v in final['violations'])!=Counter(v['type'] for v in before['violations']):
        # Exact UUID ordering can differ across native invocations. Compare counts
        # and verify the saved board bytes instead of treating order as an edit.
        if (len(final['unconnected_items'])!=len(before['unconnected_items']) or
            Counter(v['type'] for v in final['violations'])!=Counter(v['type'] for v in before['violations'])):
            raise RuntimeError('final native report changed')
    if digest(current)!=digest(a.out) or initial_hash!=digest(a.board) or initial_rules_hash!=digest(a.rules):
        raise RuntimeError('input or final copy changed')
    result=dict(scope='native-gated geometry consolidation; actual image review pending',
        source=str(a.board),source_sha256=initial_hash,output_sha256=digest(a.out),
        before_opens=len(initial['unconnected_items']),after_opens=len(final['unconnected_items']),
        before_vias=initial_audit['vias'],after_vias=old['vias'],
        before_subwidth=initial_audit['electrical']['subwidth_track_count'],
        after_subwidth=old['electrical']['subwidth_track_count'],
        accepted_transactions=sum(e['accepted'] for e in events),events=events,termination=reason,
        remaining_candidates=len(queue),electrical_qualified=old['electrical'].get('qualified',False))
    save(a.report,result)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('board',type=Path);p.add_argument('--rules',required=True,type=Path)
    p.add_argument('--out',type=Path);p.add_argument('--report',required=True,type=Path);p.add_argument('--work-dir',type=Path)
    p.add_argument('--kicad-python',default=os.environ.get('PNR_KICAD_PYTHON',sys.executable));p.add_argument('--kicad-cli',default=os.environ.get('PNR_KICAD_CLI','kicad-cli'))  # PNR_KICAD_PYTHON/PNR_KICAD_CLI (src15)
    p.add_argument('--radius',type=float,default=5.);p.add_argument('--max-trials',type=int,default=12)
    p.add_argument('--worker',choices=['inventory','apply','prune','fill','audit'])
    p.add_argument('--transaction',type=Path);p.add_argument('--prune',action='append',default=[]);a=p.parse_args()
    a.board=a.board.resolve();a.rules=a.rules.resolve()
    if a.worker:
        # Headless by design: never bootstrap a wx App here. On macOS it enters the
        # Cocoa event loop and can block forever when no GUI session is available.
        import pcbnew as k
        from pnr.fab_profile import load_board  # custom rules in force for the 'fill' worker
        board=load_board(a.board);execute_native(a,read(a.rules),board)
    else:
        result=consolidate(a)
        print(json.dumps({k:result[k] for k in ['before_opens','after_opens','before_vias','after_vias','accepted_transactions','termination']}))
if __name__=='__main__':main()
