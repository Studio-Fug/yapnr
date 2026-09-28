"""PNR_PAIR_PARALLEL: concurrent pair trials commit the serial (lowest-index) choice.

No KiCad: the worker seams (_invoke for serial/inventory/screen, _trial for
parallel trials) are replaced by scripted fakes with controlled timings.
"""
import json,os,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest import mock

import pnr.paired_bootstrap as pb

ROT=[0,90,180,270,45]


class FakeWorkers:
    """plan[ti]=(delay_s,status,accepted,error). Records every trial launch."""
    def __init__(self,plan):
        self.plan=plan;self.lock=threading.Lock();self.calls=[];self.stopped=set();self.active=0;self.peak=0
    def invoke(self,args,log,env):
        if '--worker' in args:
            Path(args[args.index('--report')+1]).write_text(json.dumps(dict(targets=[dict(net='P',source='J1.1',target='U1.1')],bounds=[0,0,10,10])))
            log.write_text('inventory\n')
        elif '--placement-candidates' in args:
            out=Path(args[args.index('--out-dir')+1]);out.mkdir(parents=True,exist_ok=False)
            proposals=json.loads(Path(args[args.index('--placement-candidates')+1]).read_text())
            (out/'result.json').write_text(json.dumps(dict(status='placement_screen',proposals=proposals)));log.write_text('screen\n')
        else:self.trial(args,log,env,None)
    def trial(self,args,log,env,stop):
        out=Path(args[args.index('--out-dir')+1]);ti=int(out.name.split('-')[1]);delay,status,accepted,error=self.plan[ti]
        spec=args[args.index('--placement-spec')+1] if '--placement-spec' in args else None
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
            self.calls.append(dict(ti=ti,board=args[3],out=str(out),log=str(log),spec=spec,seconds=float(args[args.index('--seconds')+1]),board_text=Path(args[3]).read_text()))
        try:
            log.write_text(f'trial {ti}\n');end=time.monotonic()+delay
            while time.monotonic()<end:
                if stop is not None and stop.is_set():
                    with self.lock:self.stopped.add(ti)
                    return False
                time.sleep(.005)
            out.mkdir(parents=True,exist_ok=False)  # a shared/reused out-dir would fail here
            if error:raise subprocess.CalledProcessError(3,args)
            (out/'result.json').write_text(json.dumps(dict(status=status,accepted=accepted,segments=[f'seg{ti}'])))
            if accepted:(out/'candidate.kicad_pcb').write_text(f'CANDIDATE-{ti}');(out/'candidate.kicad_pro').write_text('{}')
            return True
        finally:
            with self.lock:self.active-=1


def fail(delay=.05):return (delay,'time_budget',False,False)
def accept(delay=.05):return (delay,'routed',True,False)
def crash(delay=.05):return (delay,None,False,True)


class PairBootstrapParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        src=self.root/'src';src.mkdir()
        (src/'board.kicad_pcb').write_text('BOARD-0');(src/'board.kicad_pro').write_text('{}')
        (src/'rules.json').write_text(json.dumps(dict(diff_pairs=[dict(name='usb',p='P',n='N')],routed_pair_references=[])))
        self.board=src/'board.kicad_pcb';self.rules=src/'rules.json'
    def tearDown(self):self.tmp.cleanup()

    def route(self,plan,name,parallel,workers=3):
        fake=FakeWorkers(plan)
        env={k:v for k,v in os.environ.items() if not k.startswith('PNR_PAIR_PARALLEL')}
        if parallel:env.update(PNR_PAIR_PARALLEL='1',PNR_PAIR_PARALLEL_WORKERS=str(workers))
        def no_parallel(*a,**k):raise AssertionError('serial mode must not use the parallel trial runner')
        proposals=[dict(ref='D2',rotation=r,position=[i,0],score=i) for i,r in enumerate(ROT)]
        with mock.patch.dict(os.environ,env,clear=True),mock.patch.object(pb,'_invoke',fake.invoke),\
             mock.patch.object(pb,'_trial',fake.trial if parallel else no_parallel),mock.patch.object(pb,'pair_placements',lambda *a:proposals):
            started=time.monotonic()
            try:final=pb.run(self.board,self.rules,'constraints.yaml',self.root/name,'kpy','kcli',seconds=60,attempts=5,search_seconds=90)
            finally:fake.wall=time.monotonic()-started
        return final,fake,json.loads((self.root/name/'result.json').read_text())

    def test_lowest_index_wins_even_when_a_later_trial_accepts_first(self):
        plan=[fail(),accept(.6),accept(.2),accept(3),fail(3)]  # 0,1,2 start; 0 frees a slot for 3
        final,fake,result=self.route(plan,'par',True)
        self.assertEqual(final.read_text(),'CANDIDATE-1')
        self.assertEqual(json.loads((final.parent/'paired-reference.json').read_text()),[dict(pair='usb',segments=['seg1'])])
        self.assertEqual(json.loads((final.parent/'rules.json').read_text())['routed_pair_references'],[dict(pair='usb',segments=['seg1'])])
        ev={e['trial']:e for e in result['events']}
        self.assertEqual(sorted(ev),[0,1,2,3,4])
        self.assertEqual((ev[0]['status'],ev[0]['accepted']),('time_budget',False))
        self.assertTrue(ev[1]['accepted']);self.assertNotIn('ignored',ev[1])
        self.assertTrue(ev[2]['accepted']);self.assertTrue(ev[2]['ignored'])  # finished first, still superseded
        for ti in (3,4):self.assertEqual(ev[ti]['status'],'cancelled');self.assertTrue(ev[ti]['ignored']);self.assertFalse(ev[ti]['accepted'])
        self.assertEqual(fake.stopped,{3})  # trial 3 was running when 2 accepted; trial 4 is never launched
        self.assertNotIn(4,[c['ti'] for c in fake.calls])
        self.assertLess(fake.wall,2.0)  # stopped trials did not run out their 3 s
        self.assertEqual(fake.peak,3)
        # Serial reference with the same scripted outcomes picks the same trial.
        sfinal,sfake,sresult=self.route(plan,'ser',False)
        self.assertEqual(sfinal.read_text(),final.read_text())
        self.assertEqual([c['ti'] for c in sfake.calls],[0,1])
        self.assertEqual([(e['trial'],e['status'],e['accepted']) for e in sresult['events']],[(0,'time_budget',False),(1,'routed',True)])

    def test_trial_directories_and_inputs_are_private(self):
        final,fake,result=self.route([fail(.1)]*5,'par',True,workers=5)
        self.assertEqual(fake.peak,5)
        baseline=self.root/'par'/'baseline.kicad_pcb';folder=self.root/'par'/'pair-00'
        calls=sorted(fake.calls,key=lambda c:c['ti']);self.assertEqual([c['ti'] for c in calls],[0,1,2,3,4])
        for key in ('out','log','board'):self.assertEqual(len({c[key] for c in calls}),5,key)
        for c in calls:
            ti=c['ti'];self.assertEqual(c['out'],str(folder/f'trial-{ti:02}'));self.assertEqual(c['log'],str(folder/f'trial-{ti:02}.log'))
            self.assertEqual(c['board'],str(folder/f'input-{ti:02}'/'baseline.kicad_pcb'));self.assertNotEqual(c['board'],str(baseline))
            self.assertEqual(c['board_text'],'BOARD-0');self.assertEqual(c['spec'],None if ti==0 else str(folder/f'pose-{ti:02}.json'))
            if ti:self.assertEqual(json.loads(Path(c['spec']).read_text())['rotation'],ROT[ti-1])
            self.assertLessEqual(c['seconds'],90)
        # Nothing accepted: board unchanged, every trial reported like the serial scan.
        self.assertEqual(final.read_text(),'BOARD-0')
        self.assertEqual([(e['trial'],e['status'],e['accepted']) for e in result['events']],[(i,'time_budget',False) for i in range(5)])
        self.assertFalse(any(e.get('ignored') for e in result['events']))
        sfinal,sfake,sresult=self.route([fail(.01)]*5,'ser',False)
        self.assertEqual([(e['trial'],e['status'],e['accepted']) for e in sresult['events']],[(e['trial'],e['status'],e['accepted']) for e in result['events']])
        self.assertTrue(all(c['board']==str(self.root/'ser'/'baseline.kicad_pcb') for c in sfake.calls))  # serial argv untouched

    def test_worker_errors_follow_the_serial_scan(self):
        # Lower acceptance decides first: a later crash is ignored, as serially it never runs.
        final,fake,result=self.route([accept(.3),crash(.01),fail(),fail(),fail()],'a',True)
        self.assertEqual(final.read_text(),'CANDIDATE-0')
        ev={e['trial']:e for e in result['events']}
        self.assertEqual(ev[1]['status'],'worker_error');self.assertTrue(ev[1]['ignored']);self.assertIn('CalledProcessError',ev[1]['error'])
        # The serial scan reaches the crash before any acceptance: parallel raises as well.
        with self.assertRaises(subprocess.CalledProcessError):self.route([fail(.2),crash(.01),accept(.01),fail(),fail()],'b',True)
        with self.assertRaises(subprocess.CalledProcessError):self.route([fail(.2),crash(.01),accept(.01),fail(),fail()],'c',False)

    def test_first_accepted_stops_only_later_trials(self):
        seen={};lock=threading.Lock()
        def launch(i,stop):
            end=time.monotonic()+(.4 if i==0 else .05 if i==1 else 2)
            while time.monotonic()<end:
                if stop.is_set():
                    with lock:seen[i]='stopped'
                    return 'stopped'
                time.sleep(.005)
            with lock:seen[i]='done'
            return 'yes' if i in (0,1) else 'no'
        order=[]
        outcomes,winner=pb.first_accepted(4,launch,lambda o:o=='yes',workers=3,done=lambda i,o:order.append(i))
        self.assertEqual(winner,0);self.assertEqual(seen[0],'done');self.assertEqual(seen[1],'done');self.assertEqual(seen[2],'stopped')
        self.assertIsNone(outcomes[3]);self.assertNotIn(3,seen)  # never launched once trial 1 decided
        self.assertEqual(sorted(order),[0,1,2])
        self.assertEqual(pb.first_accepted(3,lambda i,s:'no',lambda o:o=='yes',workers=2),({0:'no',1:'no',2:'no'},None))

    def test_trial_runner_stops_a_running_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=Path(tmp)/'t.log';stop=threading.Event()
            self.assertTrue(pb._trial([sys.executable,'-c','print("ok")'],log,None,stop));self.assertIn('ok',log.read_text())
            with self.assertRaises(subprocess.CalledProcessError):pb._trial([sys.executable,'-c','raise SystemExit(4)'],log,None,stop)
            threading.Timer(.3,stop.set).start();started=time.monotonic()
            self.assertFalse(pb._trial([sys.executable,'-c','import time;time.sleep(30)'],log,None,stop))
            self.assertLess(time.monotonic()-started,5);self.assertIn('trial stopped',log.read_text())


if __name__=='__main__':unittest.main(verbosity=2)
