"""Controller-boundary checks: parallel merge footprint table and PNR_STOP_AFTER_PHASE.

Native workers are a recorded protocol fixture. The fake DRC reproduces KiCad's
behaviour of reporting lib_footprint_issues for a board without fp-lib-table.
Run: PYTHONPATH=. python tests/test_src2_native-loop.py
"""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pnr import native_loop
from pnr.graph import BoardGraph,BoardOutline,Component,Pad,Net

CLEAR=('PNR_CONTROL_FILE','PNR_LIVE_DIR','PNR_DRC_SERVICE','PNR_STOP_AFTER_PHASE','PNR_POWER_BANK_REUSE','PNR_PLANE_LEAF_REPAIR','PNR_POWER_DETOUR_REPAIR','PNR_PORTAL_REPAIR','PNR_PROFILE_DIR','PNR_CANDIDATE_WORKERS')

def fixture(root):
 repo=root/'repo';adapter=repo/'hardware/tools/keyhole_region.py';adapter.parent.mkdir(parents=True);adapter.write_text('')
 source=root/'source';source.mkdir();board=source/'input.kicad_pcb';board.write_text('fixture');board.with_suffix('.kicad_pro').write_text('{}')
 (source/'fp-lib-table').write_text('(fp_lib_table (version 7)\n  (lib (name "L") (type "KiCad") (uri "${KIPRJMOD}/parts/L") (options "") (descr ""))\n)\n')
 rules=root/'rules.json';rules.write_text('{}');cc=root/'constraints.yaml';cc.write_text('{}')
 return repo,board,rules,cc

def inventory(nets):
 poses={}
 for i,_ in enumerate(nets):poses['A%d'%i]=[1+i,2];poses['B%d'%i]=[1+i,6]
 comps=[Component(ref,'fixture',(x,10-y),0,'top',(.5,.5),(.5,.5),pads=[Pad('1','n%d'%int(ref[1:]),(0,0),(.3,.3))]) for ref,(x,y) in poses.items()]
 graph=BoardGraph('fixture',comps,[Net(n,1,[('A%d'%i,'1'),('B%d'%i,'1')]) for i,n in enumerate(nets)],BoardOutline(10,10)).to_dict()
 targets=[dict(net=n,source='A%d.1'%i,target='B%d.1'%i,source_xy=poses['A%d'%i],target_xy=poses['B%d'%i],mode='signal',distance=4+i) for i,n in enumerate(nets)]
 return dict(targets=targets,footprint_poses=poses,graph=graph,item_nets={'u-'+n:n for n in nets},excluded=[],owners={},bounds=[0,0,10,10])

class Harness:
 """Fake subprocess/DRC protocol. Board text records merges as '+' suffixes."""
 def __init__(self,test,nets=()):
  self.test=test;self.nets=list(nets);self.inv=inventory(self.nets);self.drcs=[];self.calls=[]
 def opens(self,board):
  return [dict(items=[dict(uuid='u-'+n)]) for n in self.nets[Path(board).read_text().count('+'):]]
 def drc(self,cli,board,report,*args,final=False,**kw):
  board=Path(board);self.drcs.append((board,final))
  lib=[] if (board.parent/'fp-lib-table').exists() else [dict(type='lib_footprint_issues',severity='warning',items=[dict(uuid='fp')])]
  return dict(unconnected_items=self.opens(board),violations=lib)
 def invoke(self,cmd,**kw):
  cmd=[str(c) for c in cmd];self.calls.append(cmd)
  if '--worker' in cmd:
   mode=cmd[cmd.index('--worker')+1]
   data={} if mode=='prepare' else dict(preserved=True,lost_pad_entries=[],lost_connections=[],reference_failures=[]) if mode=='check' else self.inv
   Path(cmd[cmd.index('--report')+1]).write_text(json.dumps(data));return
  if cmd[1].endswith('keyhole_region.py'):
   out=Path(cmd[cmd.index('--out-dir')+1]);out.mkdir(parents=True);(out/'candidate.kicad_pcb').write_text('proposal');(out/'candidate.kicad_pro').write_text('{}')
   (out/'candidate.drc.json').write_text('{}');(out/'result.json').write_text(json.dumps(dict(status='routed',accepted=True)));return
  module=cmd[2]
  if module=='pnr.merge_additive':
   base,proposal,current,out=map(Path,cmd[3:7]);out.write_text(current.read_text()+'+');out.with_suffix('.kicad_pro').write_text('{}');return
  if module=='pnr.planes':return
  if module=='pnr.via_coalesce':
   board=Path(cmd[3]);out=Path(cmd[cmd.index('--out')+1]);out.write_text(board.read_text());out.with_suffix('.kicad_pro').write_text('{}')
   native_loop.copy_lib_table(board,out.parent);Path(cmd[cmd.index('--report')+1]).write_text('{}');return
  if module=='pnr.power_bank_stage':
   out=Path(cmd[cmd.index('--out')+1]);out.write_text('consolidated');out.with_suffix('.kicad_pro').write_text('{}')
   native_loop.copy_lib_table(Path(cmd[3]),out.parent);Path(cmd[cmd.index('--report')+1]).write_text(json.dumps(dict(accepted_transactions=1)));return
  self.test.fail('unexpected command '+' '.join(cmd))
 def run(self,argv,env=None,patches=()):
  cwd=Path.cwd()
  try:
   with patch.dict(os.environ,env or {}):
    for key in CLEAR:
     if key not in (env or {}):os.environ.pop(key,None)
    bounded=lambda cmd,**kw:(self.invoke(cmd,**kw),0)[1]  # pnr.proc.run returns an exit code
    with patch.object(native_loop.subprocess,'run',side_effect=self.invoke),patch('pnr.proc.run',side_effect=bounded),patch('pnr.native_drc.run_drc',side_effect=self.drc):
     for p in patches:p.start()
     try:return native_loop.main(argv)
     finally:
      for p in patches:p.stop()
  finally:os.chdir(cwd)

def argv(root,repo,board,rules,cc,*extra):
 return [str(board),'--repo',str(repo),'--rules',str(rules),'--constraints',str(cc),'--out-dir',str(root/'run'),'--kicad-python','fake','--kicad-cli','fake','--route-only',*extra]


class LibTableTest(unittest.TestCase):
 def test_copy_lib_table_substitutes_project_directory(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();repo,board,rules,cc=fixture(root);target=root/'out';target.mkdir()
   self.assertTrue(native_loop.copy_lib_table(board,target))
   text=(target/'fp-lib-table').read_text();self.assertNotIn('${KIPRJMOD}',text);self.assertIn(str(board.parent.resolve())+'/parts/L',text)
   self.assertFalse(native_loop.copy_lib_table(target/'missing'/'x.kicad_pcb',target))
   copied=root/'copy/board.kicad_pcb';native_loop.copy_board(board,copied)
   self.assertEqual((copied.parent/'fp-lib-table').read_text(),text);self.assertEqual(copied.read_text(),'fixture')

 def test_parallel_merges_get_footprint_table_and_pass_gate(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();repo,board,rules,cc=fixture(root);h=Harness(self,['n0','n1'])
   result=h.run(argv(root,repo,board,rules,cc,'--cycles','1'),env=dict(PNR_SINGLE_TRACK_WORKERS='2'))
   progress=json.loads((root/'run/progress.json').read_text())
   batches=[e for e in progress['events'] if e['stage']=='parallel_routes']
   self.assertEqual(len(batches),1);self.assertEqual(batches[0]['accepted'],2);self.assertEqual(batches[0]['serial_retry'],0)
   merged=[b for b,_ in h.drcs if b.name=='merged.kicad_pcb'];self.assertEqual(len(merged),2)
   expected=(root/'run/fp-lib-table').read_text();self.assertIn(str(board.parent.resolve()),expected)
   for m in merged:self.assertEqual((m.parent/'fp-lib-table').read_text(),expected)
   self.assertEqual(progress['termination'],'zero_native_opens');self.assertEqual(progress['opens'],0)
   self.assertEqual(result,root/'run/best/candidate.kicad_pcb');self.assertEqual(result.read_text(),'fixture++')
   self.assertTrue((result.parent/'fp-lib-table').exists());self.assertNotIn('stopped_after_phase',progress)


class StopAfterPhaseTest(unittest.TestCase):
 def early(self,root,label,phase_dir=True):
  repo,board,rules,cc=fixture(root);h=Harness(self);fab=root/'fab.json';fab.write_text('{}')
  def pairs(current,rules,constraints,out,*args,**kw):
   out.mkdir(parents=True);b=out/'paired.kicad_pcb';b.write_text('paired');b.with_suffix('.kicad_pro').write_text('{}')
   native_loop.copy_lib_table(Path(current),out);(out/'paired-reference.json').write_text(json.dumps([dict(pair='USB')]));return b
  def signals(current,rules,constraints,out,*args,**kw):
   out.mkdir(parents=True);b=out/'signals.kicad_pcb';b.write_text('signals');b.with_suffix('.kicad_pro').write_text('{}')
   native_loop.copy_lib_table(Path(current),out);return b
  extra=['--electrical-fab',str(fab),'--early-pairs']+(['--phase-dir',str(root/'phases')] if phase_dir else [])
  env=dict(PNR_SINGLE_TRACK_WORKERS='1',**({'PNR_STOP_AFTER_PHASE':label} if label else {}))
  result=h.run(argv(root,repo,board,rules,cc,*extra),env=env,patches=[patch('pnr.paired_bootstrap.run',side_effect=pairs),patch('pnr.staged_signal.run',side_effect=signals)])
  return result,json.loads((root/'run/progress.json').read_text()),h

 def test_stop_after_signals_publishes_best_and_skips_refinement(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();result,progress,h=self.early(root,'06-signals')
   run=root/'run';self.assertEqual(result,run/'best/candidate.kicad_pcb');self.assertEqual(result.read_text(),'signals')
   self.assertTrue((run/'best/fp-lib-table').exists());self.assertIn((result,True),h.drcs)
   self.assertEqual(progress['termination'],'stopped_after_phase');self.assertEqual(progress['stopped_after_phase'],'06-signals')
   self.assertEqual(progress['best'],str(result));self.assertEqual(progress['rounds'],[]);self.assertEqual(progress['component_scores'],{})
   self.assertEqual(progress['opens'],0);self.assertEqual(progress['initial_opens'],0);self.assertIn('native_open_nets',progress)
   self.assertFalse((run/'cycle-01').exists());self.assertFalse((run/'initial').exists());self.assertTrue((run/'stopped-inventory/inspect.json').exists())
   # Recursive early subphases never stop, and the phase snapshot is captured first.
   for name in ('early-power','early-plane','early-power-refine'):
    self.assertNotEqual(json.loads((run/name/'progress.json').read_text())['termination'],'stopped_after_phase')
   self.assertTrue((root/'phases/06-signals/phase.json').exists());self.assertFalse((root/'phases/07-native-refinement').exists())

 def test_stop_after_pairs_keeps_policy_references(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();result,progress,h=self.early(root,'02-usb-pairs',phase_dir=False)
   run=root/'run';self.assertEqual(result.read_text(),'paired');self.assertEqual(progress['stopped_after_phase'],'02-usb-pairs')
   self.assertEqual(json.loads((run/'policy/prepare.json').read_text())['routed_pair_references'],[dict(pair='USB')])
   self.assertFalse((run/'early-power').exists())

 def test_stop_after_final_phase_keeps_loop_reason_and_not_subphases(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();result,progress,h=self.early(root,'07-native-refinement')
   run=root/'run';self.assertEqual(result,run/'best/candidate.kicad_pcb');self.assertEqual(result.read_text(),'signals')
   self.assertEqual(progress['termination'],'stopped_after_phase');self.assertEqual(progress['loop_termination'],'zero_native_opens')
   for name in ('early-power','early-plane','early-power-refine'):
    self.assertEqual(json.loads((run/name/'progress.json').read_text())['termination'],'zero_native_opens')

 def test_unset_is_unchanged(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();result,progress,h=self.early(root,None)
   self.assertEqual(progress['termination'],'zero_native_opens');self.assertNotIn('stopped_after_phase',progress);self.assertNotIn('loop_termination',progress)
   self.assertFalse((root/'run/stopped-inventory').exists());self.assertTrue((root/'phases/07-native-refinement').exists())

 def test_stop_after_bank_consolidation(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();repo,board,rules,cc=fixture(root);h=Harness(self);fab=root/'fab.json';fab.write_text('{}')
   result=h.run(argv(root,repo,board,rules,cc,'--electrical-fab',str(fab)),env=dict(PNR_POWER_BANK_REUSE='1',PNR_STOP_AFTER_PHASE='06b-power-bank-consolidation'))
   progress=json.loads((root/'run/progress.json').read_text())
   self.assertEqual(result.read_text(),'consolidated');self.assertEqual(progress['termination'],'stopped_after_phase')
   self.assertEqual([e['phase'] for e in progress['events']],['06b-power-bank-consolidation']);self.assertFalse((root/'run/08b-power-bank-consolidation').exists())

 def test_unknown_label_fails_before_work(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory).resolve();repo,board,rules,cc=fixture(root);h=Harness(self)
   with self.assertRaises(SystemExit):h.run(argv(root,repo,board,rules,cc),env=dict(PNR_STOP_AFTER_PHASE='06-signal'))
   self.assertFalse((root/'run').exists());self.assertEqual(h.calls,[])

if __name__=='__main__':unittest.main()
