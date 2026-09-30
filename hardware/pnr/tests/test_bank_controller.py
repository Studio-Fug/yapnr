import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pnr import native_loop

class BankControllerTest(unittest.TestCase):
 def run_case(self,enabled=True,fab=True,only=None,fail=False,lost=False,violation=False):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);repo=root/'repo';adapter=repo/'hardware/tools/keyhole_region.py';adapter.parent.mkdir(parents=True);adapter.write_text('');board=root/'input.kicad_pcb';board.write_text('original');board.with_suffix('.kicad_pro').write_text('{}');rules=root/'rules.json';rules.write_text('{}');cc=root/'constraints.yaml';cc.write_text('{}');f=root/'fab.json';f.write_text('{}');calls=[]
   inv=dict(targets=[],footprint_poses={},item_nets={},excluded=[])
   def invoke(cmd,**kw):
    if '--worker' in cmd:
     mode=cmd[cmd.index('--worker')+1];data={} if mode=='prepare' else dict(preserved=not lost,lost_pad_entries=['pad'] if lost else [],reference_failures=[]) if mode=='check' else inv
     Path(cmd[cmd.index('--report')+1]).write_text(json.dumps(data));return
    self.assertIn('pnr.power_bank_stage',cmd);calls.append(cmd)
    if fail:raise subprocess.CalledProcessError(23,cmd)
    out=Path(cmd[cmd.index('--out')+1]);out.write_text('consolidated');out.with_suffix('.kicad_pro').write_text('{}');Path(cmd[cmd.index('--report')+1]).write_text(json.dumps(dict(accepted_transactions=1)))
   def drc(cli,board,*args,**kw):return dict(unconnected_items=[],violations=[dict(type='clearance')] if violation and Path(board).read_text()=='consolidated' else [])
   argv=[str(board),'--repo',str(repo),'--rules',str(rules),'--constraints',str(cc),'--out-dir',str(root/'run'),'--kicad-python','fake','--kicad-cli','fake','--route-only','--cycles','1']
   if fab:argv+=['--electrical-fab',str(f)]
   if only:argv+=['--only-mode',only]
   cwd=Path.cwd()
   try:
    with patch.dict(os.environ,dict(PNR_POWER_BANK_REUSE='1' if enabled else '0')),patch.object(native_loop.subprocess,'run',side_effect=invoke),patch('pnr.native_drc.run_drc',side_effect=drc):result=native_loop.main(argv)
   finally:os.chdir(cwd)
   data=json.loads((root/'run/progress.json').read_text());return result.read_text(),calls,data
 def test_runs_before_and_after_refinement_with_compiled_policy(self):
  text,calls,d=self.run_case();self.assertEqual(text,'consolidated');self.assertEqual(len(calls),2)
  self.assertTrue(all(c[c.index('--rules')+1].endswith('/policy/prepare.json') for c in calls));self.assertEqual([e['phase'] for e in d['events']],['06b-power-bank-consolidation','08b-power-bank-consolidation'])
 def test_opt_in(self):self.assertEqual(self.run_case(enabled=False)[1],[])
 def test_requires_source_electrical_policy(self):self.assertEqual(self.run_case(fab=False)[1],[])
 def test_no_recursive_subphase_consolidation(self):self.assertEqual(self.run_case(only='power')[1],[])
 def test_worker_crash_keeps_original(self):
  text,calls,d=self.run_case(fail=True);self.assertEqual(text,'original');self.assertTrue(all(e['status']=='worker_error' and not e['accepted'] for e in d['events']))
 def test_outer_gate_rejects_lost_connectivity(self):
  text,calls,d=self.run_case(lost=True);self.assertEqual(text,'original');self.assertTrue(all(not e['accepted'] for e in d['events']))
 def test_outer_gate_rejects_native_clearance(self):
  text,calls,d=self.run_case(violation=True);self.assertEqual(text,'original');self.assertTrue(all(not e['accepted'] for e in d['events']))
if __name__=='__main__':unittest.main()
