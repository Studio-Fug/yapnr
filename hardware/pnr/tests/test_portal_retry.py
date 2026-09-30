import unittest
from unittest.mock import patch
from pnr.portal_retry import retry_command
from pnr.route.detail import portal_joint as p
from pnr.route.detail.regional import Request
class RetryTest(unittest.TestCase):
 def cmd(self):return ['python','keyhole_region.py','board','--out-dir','old','--joint','--layers','--net','a','--net','b','--pitch','.1','--max-expansions','150000','--max-seconds','20']
 def test_clamp_and_preserve(self):
  source=self.cmd();out=retry_command(source,'new',240)
  self.assertEqual(out[out.index('--max-seconds')+1],'200.0');self.assertEqual(out[out.index('--pitch')+1],'.05');self.assertIn('--portal-joint',out);self.assertEqual(source,self.cmd());self.assertEqual(out.count('--net'),2)
 def test_450_second_cap(self):
  out=retry_command(self.cmd(),'new',10000);self.assertEqual(out[out.index('--max-seconds')+1],'450.0')
 def test_reject_no_time(self):
  for budget in [0,139,float('inf'),float('nan')]:self.assertIsNone(retry_command(self.cmd(),'new',budget))
 def test_no_single_or_protected_worker(self):
  cmd=self.cmd();del cmd[cmd.index('--net'):cmd.index('--net')+2];self.assertIsNone(retry_command(cmd,'new',500));self.assertIsNone(retry_command(['python','-m','pnr.native_electrical'],'new',500));self.assertIsNone(retry_command(self.cmd()+['--preserve-copper'],'new',500))
 def test_composed_events_are_in_final_report(self):
  r=Request('a','A',[(0,0)],[(2,0)])
  result=p.solve_portal_region([r],(-1,-1,3,1),lambda r,la,a,b:la==1,lambda *a:False,terminal_layers=lambda r,p:(1,),pitch=.2,max_expansions=1000)
  self.assertEqual(result.status,'routed');events=[e for e in result.attempts if e.get('stage')=='composed_vias'];self.assertEqual(len(events),1);self.assertEqual(events[0]['count'],0)
if __name__=='__main__':unittest.main()

class ComposedBudgetTest(unittest.TestCase):
 def test_four_physical_barrels_accepted_without_dropping_composition_report(self):
  self.check_budget(5,'routed',4)
 def test_three_barrel_search_limit_rejects_same_complete_candidate(self):
  self.check_budget(3,'portal_transition_rejected',4)
 def check_budget(self,limit,status,count):
  from pnr.route.detail.regional import RegionalResult
  r=Request('a','A',[(0,0)],[(4,0)])
  body=[(0,0,1),(1,0,1),(1,0,2),(2,0,2),(2,0,1),(4,0,1)]
  def frontier(points,*args,**kwargs):return {points[0]:[points[0]]},1
  with patch.object(p,'escape_frontier',side_effect=frontier),patch.object(p,'solve_joint_region',return_value=RegionalResult('routed',{'a':body},[])):
   result=p.solve_portal_region([r],(-1,-1,5,1),lambda *a:True,lambda *a:True,max_total_vias=limit)
  self.assertEqual(result.status,status)
  events=[e for e in result.attempts if e.get('stage')=='composed_vias'];self.assertEqual(events[0]['count'],count)
  if status!='routed':self.assertEqual(result.paths,{})
