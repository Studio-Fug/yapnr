import math,unittest
from pnr.graph import BoardGraph,BoardOutline,Component,Pad,Net
from pnr.constraints import compile_constraints,ConstraintError
from pnr.place.geometry import hard_group_edges,hard_group_limits,resolve_hard_rotations
from pnr.place.legalize import legalize
from pnr.place.metrics import hard_violations,translation_checker
from pnr.place.batch_relocate import joint_configurations
from pnr.place.model import global_place

def fixture():
 g=BoardGraph('mobile',[Component('U1','',(5,5),0,'top',(2,2),(2,2),pads=[Pad('1','N',(0,0),(.4,.4))]),Component('C1','',(7,5),0,'top',(1,1),(1,1),pads=[Pad('1','N',(0,0),(.4,.4))])],[Net('N',1,[('U1','1'),('C1','1')])],BoardOutline(30,30))
 c=compile_constraints({'board':{'outline':{'w':30,'h':30}},'group':[{'anchor':'U1','members':['C1'],'radius_mm':3,'hard':True}]},g.refs)
 return g,c
class MovableGroupTest(unittest.TestCase):
 def test_anchor_and_member_can_translate_together(self):
  g,c=fixture();options={ref:[{'position':list(g.component(ref).pos),'cost':1},{'position':[g.component(ref).pos[0]+12,15],'cost':0}] for ref in ('U1','C1')}
  chosen,audit=joint_configurations(g,c,options,samples=2)
  self.assertEqual(audit['legal_configurations'],1);self.assertEqual(len(chosen),1)
  self.assertEqual(chosen[0]['graph'].component('U1').pos,(17,15))
 def test_anchor_alone_cannot_abandon_decoupler(self):
  g,c=fixture();check=translation_checker(g,c);g.component('U1').pos=(20,20)
  self.assertFalse(check(g.component('U1')));self.assertEqual(hard_violations(g,c)['group_outside'],['C1'])
 def test_legalizer_uses_actual_moved_anchor(self):
  g,c=fixture();g.component('U1').pos=(21,21)
  p=legalize(g,30,30,fixed={},keepouts=[],group_edges=hard_group_edges(c),grid_mm=.25)
  self.assertGreater(p.component('U1').pos[0],20);self.assertLessEqual(math.dist(p.component('U1').pos,p.component('C1').pos),3+1e-9)
  self.assertFalse(any(hard_violations(p,c).values()))
 def test_partial_holdout_does_not_pin_joint_group_to_old_anchor(self):
  g,c=fixture();self.assertEqual(hard_group_limits(c,{},partial=True),{})
  self.assertIn('U1',hard_group_limits(c,{'C1':(7,5)},partial=True))
 def test_sensor_orientation_does_not_fix_xy(self):
  g,c=fixture();c=compile_constraints({'board':{'outline':{'w':30,'h':30}},'orientation':{'U1':270}},g.refs)
  self.assertEqual(c.locked_refs,());self.assertEqual(resolve_hard_rotations(c),{'U1':270})
  pos,rot=global_place(g,c,30,30,seed=2,iters=8,initial_positions={'U1':(20,20),'C1':(23,20)})
  self.assertEqual(rot['U1'],270);self.assertGreater(pos['U1'][0],15)
  p=legalize(g,30,30,fixed={},keepouts=[],rotations=resolve_hard_rotations(c),allow_rotation=True)
  self.assertEqual(p.component('U1').rot,270)
  p.component('U1').rot=0;self.assertIn('U1',hard_violations(p,c)['fixed_misplaced'])
 def test_bad_orientation_or_unknown_anchor_fails(self):
  for rot in (True,45,float('inf'),float('nan')):
   with self.assertRaises(ConstraintError):compile_constraints({'orientation':{'U1':rot}},['U1'])
  with self.assertRaises(ConstraintError):compile_constraints({'group':[{'anchor':'X','members':['U1'],'radius_mm':3,'hard':True}]},['U1'])
if __name__=='__main__':unittest.main()
