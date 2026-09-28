"""Route source-declared pairs before ordinary signals claim their corridors.

Controller uses the PnR interpreter; each exact geometry/DRC transaction runs
in a fresh KiCad process. Only legal intermediate-package proposals are tried.
An unsuccessful pair remains explicitly pending for the final completeness gate.
"""
import argparse,json,os,shutil,subprocess,threading,time
from concurrent.futures import FIRST_COMPLETED,ThreadPoolExecutor,wait
from pathlib import Path
from pnr.native_loop import copy_board,pair_placements
from pnr.placement_trials import diverse_pair_poses
# PNR_PAIR_PARALLEL=1 runs a pair's trials concurrently (PNR_PAIR_PARALLEL_WORKERS,
# default 3) and commits the lowest-index accepted trial, exactly as the serial scan.


def _invoke(args,log,env):
    with log.open('w') as f:subprocess.run(args,env=env,stdout=f,stderr=subprocess.STDOUT,check=True)


def _trial(args,log,env,stop,poll=.2):
    """Cancellable _invoke for parallel trials: True if the worker finished, False if stopped."""
    with log.open('w') as f:
        proc=subprocess.Popen(args,env=env,stdout=f,stderr=subprocess.STDOUT)
        try:
            while True:
                try:code=proc.wait(timeout=poll);break
                except subprocess.TimeoutExpired:
                    if stop.is_set():return False
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                f.write('\n[paired_bootstrap] trial stopped: a lower-index trial decided this pair\n')
    if code:raise subprocess.CalledProcessError(code,args)
    return True


def first_accepted(count,launch,accepted,workers=3,done=None):
    """Run launch(i,stop) for i<count, <=workers at a time in index order; decide like the serial scan.

    Returns (outcomes,winner): winner is the lowest accepted index, else None; a
    worker error is re-raised only when the serial scan would reach it first.
    Once trial i is accepted or fails, later trials are stopped (running) or
    never launched (outcome None); lower trials always finish. done(i,outcome)
    runs on this controller thread as each launched trial ends.
    """
    stops=[threading.Event() for _ in range(count)];outcomes={};errors={};futures={};pending=set();nxt=0;cut=count
    pool=ThreadPoolExecutor(max_workers=max(1,workers))
    try:
        while True:
            while nxt<cut and len(pending)<max(1,workers):
                future=pool.submit(launch,nxt,stops[nxt]);futures[future]=nxt;pending.add(future);nxt+=1
            if not pending:break
            finished,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in sorted(finished,key=futures.get):
                i=futures[future]
                try:outcomes[i]=future.result()
                except Exception as error:errors[i]=error;outcomes[i]=dict(status='worker_error',error=repr(error))
                if i<cut and (i in errors or accepted(outcomes[i])):
                    cut=i+1  # no later trial can change the serial decision
                    for stop in stops[cut:]:stop.set()
                if done:done(i,outcomes[i])
    finally:
        for stop in stops:stop.set()
        pool.shutdown(wait=True)
    for i in range(count):outcomes.setdefault(i,None)
    for i in range(count):
        if i in errors:raise errors[i]
        if accepted(outcomes[i]):return outcomes,i
    return outcomes,None


def run(board,rules,constraints,out,kicad_python,kicad_cli,seconds=600,attempts=5,search_seconds=90,allow_placement=True):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    board=Path(board).resolve();rules=Path(rules).resolve()
    current=out/'baseline.kicad_pcb';copy_board(board,current)
    # Workers share precisely this isolated source tree, never a live checkout.
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    started=time.monotonic();events=[]
    policy=json.loads(rules.read_text());references=list(policy.get('routed_pair_references',[]))
    rules=out/'rules.json';rules.write_text(json.dumps(policy,indent=2))
    def invoke(args,log):_invoke(args,log,env)
    parallel=os.environ.get('PNR_PAIR_PARALLEL')=='1'
    workers=max(1,int(os.environ.get('PNR_PAIR_PARALLEL_WORKERS','3'))) if parallel else 1
    policy=json.loads(rules.read_text())
    for pi,pair in enumerate(policy.get('diff_pairs',[])):
        if time.monotonic()-started>=seconds:break
        folder=out/f'pair-{pi:02}';folder.mkdir()
        inv=folder/'inventory.json'
        invoke([kicad_python,'-m','pnr.native_loop',str(current),'--worker','inspect','--rules',str(rules),'--report',str(inv)],folder/'inventory.log')
        inventory=json.loads(inv.read_text())
        jobs=[t for t in inventory['targets'] if t['net'] in (pair['p'],pair['n'])]
        if not jobs:
            events.append(dict(pair=pair['name'],status='already_connected'));continue
        target=jobs[0]
        base=[kicad_python,'-m','pnr.native_electrical',str(current),'--rules',str(rules),'--net',target['net'],'--source-pad',target['source'],'--target-pad',target['target'],'--bounds',*map(str,inventory['bounds']),'--kicad-cli',kicad_cli]
        proposals=pair_placements(inventory,constraints,pair) if allow_placement else []
        pp=folder/'proposals.json';pp.write_text(json.dumps(proposals,indent=2))
        if proposals:
            screen=folder/'screen'
            invoke(base+['--out-dir',str(screen),'--placement-candidates',str(pp)],folder/'screen.log')
            proposals=json.loads((screen/'result.json').read_text())['proposals']
        if parallel:
            poses=[None]+diverse_pair_poses(proposals,attempts-1);finished={}
            def launch(ti,stop):
                if stop.is_set():return dict(status='cancelled')
                remaining=seconds-(time.monotonic()-started)
                if remaining<=0:return dict(status='budget_skipped')  # serial scan would have stopped here
                trial=folder/f'trial-{ti:02}';private=folder/f'input-{ti:02}'/current.name
                # Private input: KiCad writes project-local files (.kicad_prl) beside the board it reads.
                copy_board(current,private)
                if current.with_suffix('.kicad_dru').exists():shutil.copyfile(current.with_suffix('.kicad_dru'),private.with_suffix('.kicad_dru'))
                cmd=base[:3]+[str(private)]+base[4:]+['--out-dir',str(trial),'--seconds',str(min(search_seconds,remaining))]
                if poses[ti] is not None:
                    spec=folder/f'pose-{ti:02}.json';spec.write_text(json.dumps(poses[ti]));cmd+=['--placement-spec',str(spec)]
                t0=time.monotonic()
                if not _trial(cmd,folder/f'trial-{ti:02}.log',env,stop):return dict(status='cancelled',wall_seconds=time.monotonic()-t0)
                return dict(result=json.loads((trial/'result.json').read_text()),wall_seconds=time.monotonic()-t0)
            def trial_event(ti,outcome,winner=None):
                outcome=outcome or dict(status='cancelled');result=outcome.get('result')
                event=dict(pair=pair['name'],trial=ti,proposal=poses[ti],status=result.get('status') if result else outcome['status'],accepted=result.get('accepted',False) if result else False,folder=str(folder/f'trial-{ti:02}'),parallel=True)
                if 'wall_seconds' in outcome:event['wall_seconds']=outcome['wall_seconds']
                if 'error' in outcome:event['error']=outcome['error']
                if winner is not None and ti>winner:event['ignored']=True  # superseded by a lower-index acceptance
                return event
            def shown(record,winner=None):return [trial_event(ti,o,winner) for ti,o in sorted(record.items()) if (o or {}).get('status')!='budget_skipped']
            def progress(ti,outcome):
                finished[ti]=outcome
                (out/'progress.json').write_text(json.dumps(dict(events=events+shown(finished),seconds=time.monotonic()-started),indent=2))
            outcomes,winner=first_accepted(len(poses),launch,lambda o:bool(o and (o.get('result') or {}).get('accepted')),workers,progress)
            events.extend(shown(outcomes,winner))
            (out/'progress.json').write_text(json.dumps(dict(events=events,seconds=time.monotonic()-started),indent=2))
            if winner is not None:
                trial=folder/f'trial-{winner:02}';result=outcomes[winner]['result']
                current=trial/'candidate.kicad_pcb'
                references=[ref for ref in references if ref['pair']!=pair['name']]
                references.append(dict(pair=pair['name'],segments=result['segments']))
                policy['routed_pair_references']=references;rules.write_text(json.dumps(policy,indent=2))
            continue
        for ti,proposal in enumerate([None]+diverse_pair_poses(proposals,attempts-1)):
            remaining=seconds-(time.monotonic()-started)
            if remaining<=0:break
            trial=folder/f'trial-{ti:02}'
            cmd=base+['--out-dir',str(trial),'--seconds',str(min(search_seconds,remaining))]
            if proposal is not None:
                spec=folder/f'pose-{ti:02}.json';spec.write_text(json.dumps(proposal));cmd+=['--placement-spec',str(spec)]
            invoke(cmd,folder/f'trial-{ti:02}.log')
            result=json.loads((trial/'result.json').read_text())
            events.append(dict(pair=pair['name'],trial=ti,proposal=proposal,status=result['status'],accepted=result.get('accepted',False),folder=str(trial)))
            (out/'progress.json').write_text(json.dumps(dict(events=events,seconds=time.monotonic()-started),indent=2))
            if result.get('accepted'):
                current=trial/'candidate.kicad_pcb'
                references=[ref for ref in references if ref['pair']!=pair['name']]
                references.append(dict(pair=pair['name'],segments=result['segments']))
                policy['routed_pair_references']=references;rules.write_text(json.dumps(policy,indent=2))
                break
    final=out/'candidate.kicad_pcb';copy_board(current,final)
    (out/'paired-reference.json').write_text(json.dumps(references,indent=2))
    (out/'result.json').write_text(json.dumps(dict(events=events,seconds=time.monotonic()-started,board=str(final)),indent=2))
    return final


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('board');p.add_argument('--rules',required=True);p.add_argument('--constraints',required=True);p.add_argument('--out-dir',required=True);p.add_argument('--kicad-python',required=True);p.add_argument('--kicad-cli',required=True);p.add_argument('--seconds',type=float,default=600);p.add_argument('--attempts',type=int,default=5)
    a=p.parse_args();run(a.board,a.rules,a.constraints,a.out_dir,a.kicad_python,a.kicad_cli,a.seconds,a.attempts)

if __name__=='__main__':main()
