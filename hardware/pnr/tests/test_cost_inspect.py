import sys,copy,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import torch
from cost_fixture import fixture
from pnr.constraints import Constraint,Enforcement,NetClass
from pnr.place.cost_inspect import Objective,TERMS
from pnr.place.model import global_place
class Costs(unittest.TestCase):
 def config(self):
  g,cc=fixture();cc.constraints=[c for c in cc.constraints if c.kind!='fixed'];cc.constraints.extend([
   Constraint('fixed',Enforcement.HARD,('J0',),dict(at=list(g.component('J0').pos),rot=0,side='top')),
   Constraint('group',Enforcement.SOFT,('C9','U2'),dict(anchor='U2',radius_mm=3),weight=2.3),
   Constraint('edge_align',Enforcement.SOFT,('C9',),dict(edge='north'),weight=1.7),
   Constraint('keepout',Enforcement.HARD,(),dict(polygon=[[22,22],[28,22],[28,28],[22,28]]))])
  g.component('C9').pos=(39.5,25);g.component('J1').pos=(39,25)
  cc.net_classes=[NetClass('v',nets=('V',),plane_layer='In1.Cu'),NetClass('g',nets=('G',),plane_layer='In2.Cu')]
  return g,cc
 def test_total_matches_actual_optimizer_loss(self):
  g,cc=self.config();actual=[];original=torch.Tensor.backward
  def capture(t,*args,**kw):actual.append(float(t));return original(t,*args,**kw)
  with patch.object(torch.Tensor,'backward',capture):global_place(g,cc,40,40,iters=1,lr=0,orient=False,initial_positions={c.ref:c.pos for c in g.components},spread=1.2,inflation={'C9':1.4})
  model=Objective(g,cc,parameters={'spread':1.2},inflation={'C9':1.4});value=model.report()['board_total'];self.assertAlmostEqual(value,actual[0],delta=.01)
 def test_component_terms_sum_to_total_and_board(self):
  g,cc=self.config();r=Objective(g,cc).report();self.assertAlmostEqual(r['board_total'],sum(v['total'] for v in r['components'].values()),places=7)
  for c in r['components'].values():self.assertAlmostEqual(c['total'],sum(t['weighted'] for t in c['terms']),places=8)
 def test_vector_field_exact_global_delta(self):
  g,cc=fixture();m=Objective(g,cc);before=g.to_json();f=m.field('C9',5.);g2=copy.deepcopy(g);g2.component('C9').pos=tuple(f['origin']);target=Objective(g2,cc).report()['board_total'];self.assertAlmostEqual(f['values'][0],target-m.report()['board_total'],places=7);self.assertEqual(g.to_json(),before)
 def test_fixed_field_marks_all_moves_unavailable(self):
  g,cc=fixture();f=Objective(g,cc).field('U2',5.);self.assertTrue(f['fixed']);self.assertFalse(any(f['legal']))
 def test_no_cap_term_without_annotation(self):
  g,cc=fixture();r=Objective(g,cc).report();self.assertTrue(all(c['terms'][-1]['weighted']==0 for c in r['components'].values()))
 def test_bad_parameter_rejected(self):
  g,cc=fixture()
  for p in ({'gamma':0},{'banana':1},{'w_keep':float('nan')}):
   with self.assertRaises(ValueError):Objective(g,cc,parameters=p)
if __name__=='__main__':unittest.main()
